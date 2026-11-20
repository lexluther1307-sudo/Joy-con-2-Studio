"""Joy-Con 2 Studio setup for Windows.

It installs the Windows app, the Steam Deck plugin and the Linux files, and adds the virtual controller driver
(ViGEmBus) when it is missing. tools/build_installer.py puts the app, the plugin and the Linux packages inside
the setup program, so this file is the window and the install steps only.

  JoyCon2Studio-Setup.exe                     the window
  JoyCon2Studio-Setup.exe --silent [options]  a scripted install (see --help)
  JoyCon2Studio-Setup.exe --uninstall         the window, on the removal page
"""

import argparse
import base64
import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
import zipfile
from tkinter import filedialog

try:
    import winreg
except ImportError:  # not Windows: the install steps cannot run, but the module still imports for the tests
    winreg = None

APP_NAME = "Joy-Con 2 Studio"
APP_EXE = "JoyCon2Studio.exe"
UNINSTALLER = "Uninstall Joy-Con 2 Studio.exe"
PUBLISHER = "Joy-Con 2 Studio (unofficial)"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\JoyCon2Studio"
VIGEM_API = "https://api.github.com/repos/nefarius/ViGEmBus/releases/latest"
VIGEM_PAGE = "https://github.com/nefarius/ViGEmBus/releases/latest"
DECK_USER = "deck"
DECK_STAGING = "joycon2studio-staging"
HOST_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
DETACHED = 0x00000008  # DETACHED_PROCESS
LOG_FILE = os.path.join(tempfile.gettempdir(), "JoyCon2Studio-Setup.log")
UPDATE_FILE_TEXT = '{"auto_update": false, "last_check": 0}\n'
TEST_MODE = os.environ.get("JOYCON2STUDIO_SETUP_TEST") == "1"  # for the tests: no shortcuts, no driver, no Settings entry

# ---- colours: Material 3 dark scheme, with a glass key for the see-through parts -----------------------------
GLASS_KEY = "#010203"  # the colour Tk makes see-through; nothing else in the setup uses it
BG_SOLID = "#141218"
FIELD = "#2B2930"
BORDER = "#8D8799"
OUTLINE = "#938F99"
TEXT = "#F4EFF4"
MUTED = "#CAC4D0"
PRIMARY = "#D0BCFF"
ON_PRIMARY = "#381E72"
PRIMARY_CONTAINER = "#4F378B"
ON_PRIMARY_CONTAINER = "#EADDFF"
ERROR = "#F2B8B5"
ERROR_CONTAINER = "#8C1D18"
ON_ERROR_CONTAINER = "#F9DEDC"
GOOD = "#A8DAB5"
ACCENT = "#7DD3FC"
PINK = "#F0ABFC"
HERO_START = "#3A1B9E"
HERO_MIDDLE = "#7A2FA8"
HERO_END = "#0B6E8F"
GLOW = "#FF5FA8"
LEFT_JOYCON = "#00B8E6"
RIGHT_JOYCON = "#FF4E42"


def windows_build() -> int:
    try:
        return sys.getwindowsversion().build
    except AttributeError:
        return 0


def glass_supported() -> bool:
    return windows_build() >= 22621  # Windows 11 22H2 and later have the system backdrops


BG = GLASS_KEY if glass_supported() else BG_SOLID
SURFACE = GLASS_KEY if glass_supported() else "#211F26"
FONT = "Segoe UI Variable Text"
DISPLAY = "Segoe UI Variable Display"
MONO = "Cascadia Mono"


# ---- files and paths -----------------------------------------------------------------------------------------
def payload_dir() -> str:
    """The files the setup carries: inside the program when it is built, and the build folder when run from source."""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "payload")  # type: ignore[attr-defined]
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, os.pardir, "build", "installer", "payload"))


def setup_version() -> str:
    with open(os.path.join(payload_dir(), "version.txt"), encoding="utf-8") as handle:
        return handle.read().strip()


def default_install_dir() -> str:
    if os.environ.get("JOYCON2STUDIO_SETUP_DIR"):  # for the tests: a folder of their own
        return os.environ["JOYCON2STUDIO_SETUP_DIR"]
    return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Programs", APP_NAME)


def default_linux_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "Documents", APP_NAME, "Linux")


def deck_plugin_dir(target: str) -> str:
    return os.path.join(target, "Steam Deck plugin")


def shell_folder(name: str) -> str:
    """The Desktop or Start menu folder as Windows has it now (it can be moved, for example by OneDrive)."""
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
    try:
        value, _ = winreg.QueryValueEx(key, name)
    finally:
        winreg.CloseKey(key)
    return os.path.expandvars(value)


def start_menu_shortcut() -> str:
    return os.path.join(shell_folder("Programs"), APP_NAME + ".lnk")


def desktop_shortcut() -> str:
    return os.path.join(shell_folder("Desktop"), APP_NAME + ".lnk")


def valid_host(host: str) -> bool:
    return bool(HOST_PATTERN.match(host or ""))


# ---- logging ---------------------------------------------------------------------------------------------------
def write_log(text: str) -> str:
    line = f"{time.strftime('%H:%M:%S')}  {text}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    return line


# ---- PowerShell helper (used for shortcuts and the driver signature check) -------------------------------------
def run_powershell(script: str, values: dict) -> str:
    env = dict(os.environ, **values)  # values travel as environment variables, so no quoting problems
    result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                            env=env, capture_output=True, text=True, creationflags=NO_WINDOW)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or f"PowerShell stopped with {result.returncode}")
    return result.stdout.strip()


def make_shortcut(lnk: str, exe: str) -> None:
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:JCS_LNK);"
              "$s.TargetPath = $env:JCS_TARGET;"
              "$s.WorkingDirectory = [IO.Path]::GetDirectoryName($env:JCS_TARGET);"
              "$s.IconLocation = $env:JCS_TARGET + ',0';"
              "$s.Description = 'Joy-Con 2 Studio';"
              "$s.Save()")
    run_powershell(script, {"JCS_LNK": lnk, "JCS_TARGET": exe})


def remove_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ---- the Windows app -----------------------------------------------------------------------------------------
def running_copies(target: str) -> list:
    """Paths of running copies of the app that live in the target folder."""
    if not sys.platform.startswith("win"):
        return []
    script = "Get-Process -Name JoyCon2Studio -ErrorAction SilentlyContinue | ForEach-Object { $_.Path }"
    try:
        found = run_powershell(script, {}).splitlines()
    except RuntimeError:
        return []
    return [path for path in found if path and path.lower().startswith(target.lower())]


def copy_app(target: str) -> None:
    os.makedirs(target, exist_ok=True)
    try:
        shutil.copy2(os.path.join(payload_dir(), APP_EXE), os.path.join(target, APP_EXE))
    except PermissionError as exc:
        raise RuntimeError("Joy-Con 2 Studio is open. Close it, then run the setup again.") from exc
    shutil.copy2(os.path.join(payload_dir(), "LICENSE"), os.path.join(target, "LICENSE.txt"))
    with open(os.path.join(target, "version.txt"), "w", encoding="utf-8") as handle:
        handle.write(setup_version() + "\n")
    if getattr(sys, "frozen", False):
        shutil.copy2(sys.executable, os.path.join(target, UNINSTALLER))


def write_app_settings(target: str, check_updates: bool) -> None:
    """Sets the update check in the app's settings and keeps everything else in the file."""
    path = os.path.join(target, "settings.json")
    data = {}
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
        data = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        data = {}
    data["check_updates"] = bool(check_updates)
    os.makedirs(target, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)


def uninstall_values(target: str, version_text: str, uninstaller: str) -> dict:
    return {
        "DisplayName": APP_NAME,
        "DisplayVersion": version_text,
        "Publisher": PUBLISHER,
        "InstallLocation": target,
        "DisplayIcon": os.path.join(target, APP_EXE),
        "UninstallString": f'"{uninstaller}" --uninstall --dir "{target}"',
    }


def register_app(target: str) -> None:
    """Adds the app to Settings > Apps for this user, so Windows can remove it."""
    uninstaller = os.path.join(target, UNINSTALLER)
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, winreg.KEY_WRITE)
    try:
        for name, value in uninstall_values(target, setup_version(), uninstaller).items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
    finally:
        winreg.CloseKey(key)


def unregister_app() -> None:
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass


def installed_copy():
    """(folder, version) of an installed copy, or None."""
    if winreg is None:
        return None
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        return None
    try:
        location, _ = winreg.QueryValueEx(key, "InstallLocation")
        version_text, _ = winreg.QueryValueEx(key, "DisplayVersion")
        return location, version_text
    except OSError:
        return None
    finally:
        winreg.CloseKey(key)


# ---- the virtual controller driver (ViGEmBus) -------------------------------------------------------------------
def vigem_installed() -> bool:
    if winreg is None:
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\ViGEmBus")
    except OSError:
        return False
    winreg.CloseKey(key)
    return True


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "joycon2studio-setup"})
    with urllib.request.urlopen(request, timeout=20) as reply:
        return json.loads(reply.read(1024 * 1024).decode("utf-8"))


def find_vigem_installer() -> tuple:
    for asset in fetch_json(VIGEM_API).get("assets", []):
        name = asset.get("name", "")
        if name.lower().startswith("vigembus") and name.lower().endswith(".exe"):
            return name, asset["browser_download_url"]
    raise RuntimeError(f"the latest ViGEmBus release has no installer ({VIGEM_PAGE})")


def download(url: str, dest: str, progress=None) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "joycon2studio-setup"})
    with urllib.request.urlopen(request, timeout=60) as reply, open(dest, "wb") as out:
        total = int(reply.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = reply.read(64 * 1024)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total)


def signature_ok(path: str) -> bool:
    script = ("$s = Get-AuthenticodeSignature -LiteralPath $env:JCS_FILE;"
              "if ($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -match 'Nefarius') { 'yes' } else { 'no' }")
    return run_powershell(script, {"JCS_FILE": path}) == "yes"


def install_driver(log, progress=None) -> bool:
    if vigem_installed():
        log("The virtual controller driver is already installed.")
        return True
    name, url = find_vigem_installer()
    path = os.path.join(tempfile.gettempdir(), name)
    log(f"Downloading {name} from the official ViGEmBus release.")
    download(url, path, progress)
    if not signature_ok(path):
        raise RuntimeError("the download is not signed by Nefarius, so it was not run")
    log("Starting the driver installer. Approve the Windows administrator prompt.")
    try:
        run_powershell("Start-Process -FilePath $env:JCS_FILE -Verb RunAs -Wait", {"JCS_FILE": path})
    except RuntimeError as exc:
        raise RuntimeError(f"the administrator prompt was not approved ({exc})") from exc
    return vigem_installed()


# ---- the Steam Deck: unpack the plugin, write the install script, open it in a console window -------------------
def unpack_deck_plugin(target: str) -> str:
    dest = deck_plugin_dir(target)
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    with zipfile.ZipFile(os.path.join(payload_dir(), "joycon2studio-plugin.zip")) as archive:
        archive.extractall(dest)  # the zip's top folder is joycon2studio/
    return os.path.join(dest, "joycon2studio")


def deck_script_text(host: str, plugin_dir: str, auto_updates: bool) -> str:
    if not valid_host(host):
        raise ValueError(f"'{host}' is not a Steam Deck address. Use its IP address or its name, such as steamdeck.local")
    target = f"{DECK_USER}@{host}"
    lines = [
        "@echo off",
        "title Joy-Con 2 Studio - install on the Steam Deck",
        "echo.",
        f"echo  Installing the Steam Deck plugin on {target}",
        "echo  Type the Deck's password if asked, then its sudo password when install.sh asks for it.",
        "echo  Nothing is saved from this window.",
        "echo.",
        f'tar -cf - -C "{plugin_dir}" . | ssh {target} "rm -rf ~/{DECK_STAGING} && mkdir -p ~/{DECK_STAGING} && tar -xf - -C ~/{DECK_STAGING}"',
        "if errorlevel 1 goto failed",
    ]
    if not auto_updates:
        # Written before install.sh restarts the plugin, which reads this file when it starts
        payload = base64.b64encode(UPDATE_FILE_TEXT.encode("ascii")).decode("ascii")
        lines.append(f'ssh {target} "mkdir -p ~/homebrew/settings/joycon2studio && echo {payload} | base64 -d > ~/homebrew/settings/joycon2studio/update.json"')
        lines.append("if errorlevel 1 goto failed")
    lines += [
        f'ssh -t {target} "cd ~/{DECK_STAGING} && bash install.sh"',
        "if errorlevel 1 goto failed",
        "echo.",
        "echo  Done. The plugin is installed on the Steam Deck. Open Quick Access and pick Joy-Con 2 Studio.",
        "goto end",
        ":failed",
        "echo.",
        "echo  The install did not finish. Read the messages above.",
        "echo  Check that Decky Loader is installed on the Deck and that SSH works, then run this again.",
        ":end",
        "echo.",
        "pause",
    ]
    return "\r\n".join(lines) + "\r\n"


def write_deck_script(target: str, plugin_dir: str, host: str, auto_updates: bool) -> str:
    os.makedirs(target, exist_ok=True)
    path = os.path.join(target, "Install on the Steam Deck.cmd")
    with open(path, "w", encoding="mbcs", newline="") as handle:
        handle.write(deck_script_text(host, plugin_dir, auto_updates))
    return path


def open_deck_window(script: str) -> None:
    subprocess.Popen(f'start "Joy-Con 2 Studio - Steam Deck" cmd /k ""{script}""', shell=True)


# ---- the Linux files ------------------------------------------------------------------------------------------
LINUX_README = """Joy-Con 2 Studio for Linux
==========================

Works on Ubuntu 24.04 or newer, and on Arch Linux.

1. Copy this whole folder to the Linux PC.
2. Open a terminal in the folder and run:

       bash install-linux.sh

The script installs what the app needs and the app itself. It asks for your password when it needs sudo.
Afterwards, log out and back in (or reboot). Then start Joy-Con 2 Studio from the application menu.
"""


def copy_linux(folder: str) -> str:
    os.makedirs(folder, exist_ok=True)
    source = os.path.join(payload_dir(), "linux")
    for name in os.listdir(source):
        shutil.copy2(os.path.join(source, name), folder)
    with open(os.path.join(folder, "README - Linux.txt"), "w", encoding="utf-8") as handle:
        handle.write(LINUX_README)
    return folder


# ---- install and uninstall, as steps the window and the silent mode both use -----------------------------------
STEPS = [
    ("app", "Copy the Windows app"),
    ("shortcuts", "Create shortcuts"),
    ("register", "Add the app to Settings > Apps"),
    ("driver", "Install the virtual controller driver"),
    ("deck_unpack", "Unpack the Steam Deck plugin"),
    ("deck_script", "Write the Steam Deck install script"),
    ("deck_open", "Open the Steam Deck window"),
    ("linux", "Copy the Linux files"),
]


def install_all(options: dict, log, progress, step) -> list:
    """Runs the chosen steps. Returns the messages of the steps that failed (empty when all went well)."""
    failures = []
    target = options["target"]
    selected = {
        "app": True,
        "shortcuts": options["start_menu"] or options["desktop"],
        "register": options["register"],
        "driver": options["driver"],
        "deck_unpack": bool(options["deck_host"]),
        "deck_script": bool(options["deck_host"]),
        "deck_open": bool(options["deck_host"]) and options["open_deck"],
        "linux": bool(options["linux_dir"]),
    }

    def run(key, action):
        if not selected[key]:
            step(key, "skipped")
            return None
        step(key, "running")
        try:
            result = action()
            step(key, "done")
            return result
        except Exception as exc:  # each step reports its own problem; the rest still run
            message = f"{dict(STEPS)[key]}: {exc}"
            failures.append(message)
            write_log(message)
            log(message)
            step(key, "failed")
            return None

    def do_app():
        copy_app(target)
        write_app_settings(target, options["check_updates"])
        log(f"Copied the app to {target}.")

    def do_shortcuts():
        exe = os.path.join(target, APP_EXE)
        if options["start_menu"]:
            make_shortcut(start_menu_shortcut(), exe)
            log("Added a Start menu shortcut.")
        if options["desktop"]:
            make_shortcut(desktop_shortcut(), exe)
            log("Added a desktop shortcut.")

    def do_register():
        if not getattr(sys, "frozen", False):
            raise RuntimeError("only the built setup can register itself (run from source, nothing was added)")
        register_app(target)
        log("The app is listed under Settings > Apps.")

    def do_driver():
        ok = install_driver(log, progress)
        if not ok:
            raise RuntimeError(f"the driver did not start. Install it by hand from {VIGEM_PAGE}")
        log("The virtual controller driver is ready.")

    def do_deck_unpack():
        options["_plugin_dir"] = unpack_deck_plugin(target)
        log("Unpacked the Steam Deck plugin.")

    def do_deck_script():
        options["_script"] = write_deck_script(target, options["_plugin_dir"], options["deck_host"], options["deck_auto_updates"])
        log(f"Wrote the Steam Deck install script: {options['_script']}")

    def do_deck_open():
        open_deck_window(options["_script"])
        log("Opened the Steam Deck window. Finish the install there, typing the passwords it asks for.")

    def do_linux():
        folder = copy_linux(options["linux_dir"])
        log(f"Copied the Linux files to {folder}.")

    run("app", do_app)
    run("shortcuts", do_shortcuts)
    run("register", do_register)
    run("driver", do_driver)
    run("deck_unpack", do_deck_unpack)
    run("deck_script", do_deck_script)
    run("deck_open", do_deck_open)
    run("linux", do_linux)
    return failures


def uninstall_copy(target: str, log) -> list:
    failures = []
    for lnk in (start_menu_shortcut(), desktop_shortcut()):
        remove_file(lnk)
    try:
        unregister_app()
        log("Removed the entry from Settings > Apps.")
    except Exception as exc:
        failures.append(f"could not remove the Settings entry: {exc}")
    busy = running_copies(target)
    if busy:
        raise RuntimeError("Joy-Con 2 Studio is open. Close it, then remove it again.")
    # The uninstaller lives inside the folder it removes, so Windows removes the folder after it has exited
    script = f'ping -n 3 127.0.0.1 >nul & rmdir /s /q "{target}"'
    subprocess.Popen(f"cmd /c {script}", shell=True, creationflags=DETACHED | NO_WINDOW)
    log(f"Removing {target} once this window closes.")
    return failures


# ---- the window -----------------------------------------------------------------------------------------------
def mix(a: str, b: str, t: float) -> str:
    ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    br, bg_, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    return "#%02x%02x%02x" % (round(ar + (br - ar) * t), round(ag + (bg_ - ag) * t), round(ab + (bb - ab) * t))


def rounded_points(x1, y1, x2, y2, r):
    return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
            x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]


def apply_glass(window) -> bool:
    """Windows 11 acrylic behind the window, with the glass key made see-through. False if Windows refuses."""
    try:
        user32 = ctypes.windll.user32
        dwmapi = ctypes.windll.dwmapi
        hwnd = user32.GetParent(window.winfo_id())

        class Margins(ctypes.Structure):
            _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int), ("top", ctypes.c_int), ("bottom", ctypes.c_int)]

        if dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(Margins(-1, -1, -1, -1))) != 0:
            return False
        acrylic = ctypes.c_int(3)  # DWMWA_SYSTEM_BACKDROP_TYPE 38: 3 = acrylic
        if dwmapi.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(acrylic), ctypes.sizeof(acrylic)) != 0:
            return False
        dark = ctypes.c_int(1)
        dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))  # dark title bar
        window.wm_attributes("-transparentcolor", GLASS_KEY)
        return True
    except (OSError, AttributeError, tk.TclError):
        return False


def draw_icon(canvas, kind: str, color: str) -> None:
    """A small drawn icon: the Windows squares, a handheld, or a terminal prompt."""
    canvas.delete("all")
    if kind == "windows":
        for x, y in ((4, 4), (25, 4), (4, 25), (25, 25)):
            canvas.create_polygon(rounded_points(x, y, x + 17, y + 17, 4), smooth=True, fill=color, outline="")
    elif kind == "deck":
        canvas.create_polygon(rounded_points(2, 9, 42, 35, 10), smooth=True, fill="", outline=color, width=3)
        canvas.create_oval(9, 17, 15, 23, fill=color, outline="")
        canvas.create_oval(29, 17, 35, 23, fill=color, outline="")
    else:
        canvas.create_text(23, 22, text=">_", fill=color, font=(MONO, 16, "bold"))


class Button(tk.Canvas):
    """A Material 3 button: filled, tonal, outlined or danger, with a state layer on hover."""

    STYLES = {
        "filled": (PRIMARY, ON_PRIMARY, None),
        "tonal": (PRIMARY_CONTAINER, ON_PRIMARY_CONTAINER, None),
        "outlined": (None, PRIMARY, OUTLINE),
        "danger": (ERROR_CONTAINER, ON_ERROR_CONTAINER, None),
    }

    def __init__(self, parent, text, command, style="tonal", width=200, height=44, bg=None):
        super().__init__(parent, width=width, height=height, bg=bg or BG, highlightthickness=0, bd=0, cursor="hand2")
        self._text, self._command, self._style = text, command, style
        self._hover = False
        self._enabled = True
        self._font = (DISPLAY, 11, "bold")
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._release)
        self._draw()

    def _set_hover(self, on):
        self._hover = on
        self._draw()

    def _release(self, event):
        if self._enabled and 0 <= event.x <= int(self["width"]) and 0 <= event.y <= int(self["height"]):
            self._command()

    def set_enabled(self, on: bool):
        self._enabled = on
        self.configure(cursor="hand2" if on else "arrow")
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = int(self["width"]), int(self["height"])
        fill, text_color, outline = self.STYLES[self._style]
        if not self._enabled:
            fill, text_color, outline = FIELD, OUTLINE, None
        elif self._hover:
            if fill:
                fill = mix(fill, text_color, 0.12)
            else:
                fill = mix(BG_SOLID, text_color, 0.10)
        if fill:
            self.create_polygon(rounded_points(1, 1, w - 1, h - 1, h / 2), smooth=True, fill=fill, outline="")
        if outline:
            self.create_polygon(rounded_points(1, 1, w - 1, h - 1, h / 2), smooth=True, fill="", outline=outline, width=1.5)
        self.create_text(w // 2, h // 2, text=self._text, fill=text_color, font=self._font)


class Switch(tk.Canvas):
    """A Material 3 switch bound to a BooleanVar."""

    def __init__(self, parent, variable, command=None, bg=None):
        super().__init__(parent, width=56, height=32, bg=bg or BG, highlightthickness=0, bd=0, cursor="hand2")
        self._var, self._command = variable, command
        self.bind("<ButtonRelease-1>", self._click)
        self._draw()

    def _click(self, _event):
        self._var.set(not self._var.get())
        self._draw()
        if self._command:
            self._command()

    def _draw(self):
        self.delete("all")
        on = self._var.get()
        track = PRIMARY if on else FIELD
        self.create_polygon(rounded_points(1, 3, 55, 29, 13), smooth=True, fill=track,
                            outline="" if on else OUTLINE, width=2)
        x = 40 if on else 16
        self.create_oval(x - 9, 16 - 9, x + 9, 16 + 9, fill=ON_PRIMARY if on else OUTLINE, outline="")


class GlassCard(tk.Canvas):
    """A rounded card that sizes itself to its content. In the glass look it is see-through, with a light edge."""

    def __init__(self, parent, width, radius=22, bg=None):
        super().__init__(parent, width=width, height=40, bg=bg or BG, highlightthickness=0, bd=0)
        self._radius = radius
        self.body = tk.Frame(self, bg=SURFACE, bd=0, highlightthickness=0)
        self.create_window(radius, radius, window=self.body, anchor="nw", width=width - 2 * radius)
        self.body.bind("<Configure>", self._fit)

    def _fit(self, _event=None):
        r = self._radius
        w = int(self.cget("width"))
        h = self.body.winfo_reqheight() + 2 * r
        if int(self.cget("height")) != h:
            self.configure(height=h)
        self.delete("edge")
        self.create_polygon(rounded_points(1, 1, w - 1, h - 1, r), smooth=True, fill=SURFACE, outline=BORDER,
                            width=1.5, tags="edge")
        self.tag_lower("edge")


class Field(tk.Frame):
    """A text entry in a dark box."""

    def __init__(self, parent, variable, width=34, bg=None):
        super().__init__(parent, bg=bg or SURFACE, highlightthickness=1, highlightbackground=OUTLINE,
                         highlightcolor=PRIMARY, bd=0)
        self.entry = tk.Entry(self, textvariable=variable, width=width, font=(FONT, 11), bg=FIELD, fg=TEXT,
                              insertbackground=PRIMARY, relief="flat", bd=0, highlightthickness=0)
        self.entry.pack(padx=12, pady=9, fill="x")


class Progress(tk.Canvas):
    """A linear progress bar, determinate or moving (when the length of the job is not known)."""

    def __init__(self, parent, width=340, height=12, bg=None):
        super().__init__(parent, width=width, height=height, bg=bg or BG, highlightthickness=0, bd=0)
        self._fraction = 0.0
        self._moving = False
        self._phase = 0.0
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, fraction):
        self._moving = False
        self._fraction = max(0.0, min(1.0, fraction))
        self._draw()

    def start_moving(self):
        self._moving = True
        self._tick()

    def stop(self):
        self._moving = False

    def _tick(self):
        if not self._moving:
            return
        self._phase = (self._phase + 0.02) % 1.0
        self._draw()
        self.after(33, self._tick)

    def _draw(self):
        self.delete("all")
        w, h = max(2, self.winfo_width()), max(2, self.winfo_height())
        self.create_polygon(rounded_points(0, 0, w, h, h / 2), smooth=True, fill=FIELD, outline="")
        if self._moving:
            span = w * 0.3
            left = (w + span) * self._phase - span
            start, end = max(0.0, left), min(float(w), left + span)
            if end > start:
                self.create_polygon(rounded_points(start, 0, end, h, h / 2), smooth=True, fill=PRIMARY, outline="")
        elif self._fraction > 0:
            fill_w = max(h, w * self._fraction)
            self.create_polygon(rounded_points(0, 0, fill_w, h, h / 2), smooth=True, fill=PRIMARY, outline="")


class Hero(tk.Canvas):
    """The top band: a violet, magenta and teal gradient with a glow, two Joy-Cons, the title and the step chips."""

    HEIGHT = 200

    def __init__(self, parent):
        super().__init__(parent, height=self.HEIGHT, bg=BG_SOLID, highlightthickness=0, bd=0)
        self.step = 0
        self.steps = ["Welcome", "Choose", "Install", "Done"]
        self.bind("<Configure>", lambda _e: self.draw())

    def draw(self, step=None):
        if step is not None:
            self.step = step
        self.delete("all")
        w, h = max(2, self.winfo_width()), self.HEIGHT
        for i in range(160):
            t = i / 159
            color = mix(HERO_START, HERO_MIDDLE, t * 2) if t < 0.5 else mix(HERO_MIDDLE, HERO_END, (t - 0.5) * 2)
            self.create_rectangle(w * i / 160, 0, w * (i + 1) / 160 + 1, h, outline="", fill=color)
        cx, cy = w - 250, 96
        for r in range(280, 20, -10):
            t = 1 - (r - 20) / 260
            self.create_oval(cx - r, cy - r, cx + r, cy + r, outline="", fill=mix(HERO_MIDDLE, GLOW, t * 0.8))
        self.create_text(56, 66, text="Joy-Con 2 Studio", anchor="w", fill="#FFFFFF", font=(DISPLAY, 34, "bold"))
        self.create_text(58, 110, text="Set up the Windows app, the Steam Deck plugin and the Linux files.",
                         anchor="w", fill="#F3E8FF", font=(FONT, 13))
        self.draw_joycon(w - 318, 100, LEFT_JOYCON)
        self.draw_joycon(w - 196, 100, RIGHT_JOYCON)
        x = 58
        for i, name in enumerate(self.steps):
            active = i == self.step
            done = i < self.step
            label = f"{i + 1}  {name}"
            pill_w = 26 + len(label) * 9
            fill = "#FFFFFF" if active else (mix(HERO_START, "#FFFFFF", 0.30) if done else mix(HERO_START, "#FFFFFF", 0.12))
            ink = BG_SOLID if active else "#F3E8FF"
            self.create_polygon(rounded_points(x, 150, x + pill_w, 182, 16), smooth=True, fill=fill, outline="")
            self.create_text(x + pill_w // 2, 166, text=label, fill=ink, font=(FONT, 10, "bold"))
            x += pill_w + 10

    def draw_joycon(self, cx, cy, color):
        self.create_polygon(rounded_points(cx - 40, cy - 74, cx + 40, cy + 74, 26), smooth=True, fill=color, outline="")
        self.create_oval(cx - 14, cy - 40, cx + 14, cy - 12, fill="#1E1E28", outline="")
        for dx, dy in ((0, 18), (12, 6), (-12, 6), (0, -6)):
            self.create_oval(cx + dx - 4.5, cy + dy - 4.5, cx + dx + 4.5, cy + dy + 4.5, fill="#1E1E28", outline="")
        self.create_rectangle(cx - 40, cy + 56, cx + 40, cy + 70, fill=mix(color, "#000000", 0.25), outline="")


class SetupWindow(tk.Tk):
    def __init__(self, uninstall_only=False):
        super().__init__()
        self.title("Joy-Con 2 Studio Setup")
        self.geometry("1080x900+100+30")
        self.minsize(1040, 860)
        self.configure(bg=BG)
        self.glass = glass_supported() and apply_glass(self)
        self.queue = queue.Queue()
        self.options = self._default_options()
        self.installed = installed_copy()
        self.failures = []
        self.footer = tk.Frame(self, bg=BG)
        self.footer.pack(side="bottom", fill="x", padx=40, pady=(8, 22))
        self.hero = Hero(self)
        self.hero.pack(side="top", fill="x")
        self.body = tk.Frame(self, bg=BG)
        self.body.pack(side="top", fill="both", expand=True, padx=40, pady=(18, 8))
        self.screens = {}
        self.current = None
        self.step_labels = {}
        self._build_welcome()
        self._build_options()
        self._build_install()
        self._build_done()
        self._build_uninstall()
        self.after(40, self._pump)
        self.show("uninstall" if uninstall_only and self.installed else "welcome")

    # the options the window collects, with the defaults for this PC
    def _default_options(self):
        return {
            "target": tk.StringVar(value=default_install_dir()),
            "start_menu": tk.BooleanVar(value=not TEST_MODE),
            "desktop": tk.BooleanVar(value=not TEST_MODE),
            "register": tk.BooleanVar(value=not TEST_MODE),
            "driver": tk.BooleanVar(value=not TEST_MODE and not vigem_installed()),
            "deck": tk.BooleanVar(value=False),
            "deck_host": tk.StringVar(value=""),
            "deck_auto": tk.BooleanVar(value=False),
            "open_deck": tk.BooleanVar(value=True),
            "linux": tk.BooleanVar(value=False),
            "linux_dir": tk.StringVar(value=default_linux_dir()),
            "check_updates": tk.BooleanVar(value=False),
        }

    def show(self, name):
        for frame in self.screens.values():
            frame.pack_forget()
        self.current = name
        self.screens[name].pack(fill="both", expand=True)
        self.hero.draw({"welcome": 0, "options": 1, "install": 2, "done": 3, "uninstall": 0}[name])
        self._footer_for(name)

    def _footer_for(self, name):
        for child in self.footer.winfo_children():
            child.destroy()
        if name == "welcome":
            Button(self.footer, "Choose what to install", lambda: self.show("options"), "filled", 300, 52, BG).pack(side="right")
            if self.installed:
                Button(self.footer, "Uninstall", lambda: self.show("uninstall"), "danger", 170, 52, BG).pack(side="left")
        elif name == "options":
            Button(self.footer, "Back", lambda: self.show("welcome"), "outlined", 150, 52, BG).pack(side="left")
            Button(self.footer, "Install", self._start_install, "filled", 240, 52, BG).pack(side="right")
        elif name == "uninstall":
            Button(self.footer, "Back", lambda: self.show("welcome"), "outlined", 150, 52, BG).pack(side="left")
            Button(self.footer, "Remove Joy-Con 2 Studio", self._start_uninstall, "danger", 320, 52, BG).pack(side="right")
        elif name == "install":
            wait = Button(self.footer, "Installing...", lambda: None, "tonal", 220, 52, BG)
            wait.set_enabled(False)
            wait.pack(side="right")
        elif name == "done":
            Button(self.footer, "Finish", self.destroy, "tonal", 170, 52, BG).pack(side="right")
            exe = os.path.join(self.options["target"].get(), APP_EXE)
            if not self.failures and os.path.exists(exe):
                Button(self.footer, "Open Joy-Con 2 Studio", lambda: subprocess.Popen([exe]), "filled", 300, 52, BG).pack(side="right", padx=(0, 12))

    # ---- welcome -------------------------------------------------------------------------------------------
    def _build_welcome(self):
        page = tk.Frame(self.body, bg=BG)
        self.screens["welcome"] = page
        tk.Label(page, text="Set up Joy-Con 2 Studio", font=(DISPLAY, 26, "bold"), fg=TEXT, bg=BG).pack(anchor="w")
        tk.Label(page, text="This installs the Windows app. Add the Steam Deck plugin and the Linux files if you want them.\n"
                            "The only admin prompt is for the virtual controller driver, and only when it is missing.",
                 font=(FONT, 11), fg=MUTED, bg=BG, justify="left").pack(anchor="w", pady=(6, 26))
        row = tk.Frame(page, bg=BG)
        row.pack(fill="x")
        app_state = f"Installed: version {self.installed[1]}" if self.installed else "Not installed yet"
        self._welcome_card(row, "windows", "Windows app", "Joy-Con 2 Studio for Windows", app_state, PRIMARY, 0)
        self._welcome_card(row, "deck", "Steam Deck plugin", "Shows the Joy-Cons to Steam on the Deck",
                           "Install over SSH, from this setup", ACCENT, 1)
        self._welcome_card(row, "linux", "Linux", "Ubuntu 24.04 or newer, and Arch Linux",
                           "Run install-linux.sh on the Linux PC", PINK, 2)
        needs = GlassCard(page, 1000, radius=20, bg=BG)
        needs.pack(fill="x", pady=(26, 0))
        tk.Label(needs.body, text="What you need", font=(DISPLAY, 12, "bold"), fg=TEXT, bg=SURFACE).pack(anchor="w")
        tk.Label(needs.body, text="Windows 10 or 11 with Bluetooth LE   ·   Joy-Con 2 Left and Right   ·   "
                                  "Steam Deck: Decky Loader and SSH   ·   Linux: Ubuntu 24.04 or newer, or Arch",
                 font=(FONT, 10), fg=MUTED, bg=SURFACE, wraplength=900, justify="left").pack(anchor="w", pady=(4, 0))

    def _welcome_card(self, parent, icon, title, subtitle, state, color, column):
        card = GlassCard(parent, 312, radius=22, bg=BG)
        card.grid(row=0, column=column, padx=(0 if column == 0 else 12, 0), sticky="n")
        parent.grid_columnconfigure(column, weight=1)
        head = tk.Frame(card.body, bg=SURFACE)
        head.pack(anchor="w", fill="x")
        glyph = tk.Canvas(head, width=48, height=44, bg=SURFACE, highlightthickness=0, bd=0)
        glyph.pack(side="left")
        draw_icon(glyph, icon, color)
        tk.Label(head, text=title, font=(DISPLAY, 14, "bold"), fg=TEXT, bg=SURFACE).pack(side="left", padx=(8, 0))
        tk.Label(card.body, text=subtitle, font=(FONT, 10), fg=MUTED, bg=SURFACE, wraplength=270,
                 justify="left").pack(anchor="w", pady=(10, 0))
        tk.Label(card.body, text=state, font=(FONT, 10, "bold"), fg=GOOD, bg=SURFACE, wraplength=270,
                 justify="left").pack(anchor="w", pady=(10, 0))

    # ---- options -------------------------------------------------------------------------------------------
    def _build_options(self):
        page = tk.Frame(self.body, bg=BG)
        self.screens["options"] = page
        columns = tk.Frame(page, bg=BG)
        columns.pack(fill="both", expand=True)
        left = tk.Frame(columns, bg=BG)
        right = tk.Frame(columns, bg=BG)
        left.pack(side="left", fill="both", expand=True, padx=(0, 12), anchor="n")
        right.pack(side="left", fill="both", expand=True, padx=(12, 0), anchor="n")
        o = self.options

        app = GlassCard(left, 472, radius=24, bg=BG)
        app.pack(fill="x", pady=(0, 14))
        self._section(app.body, "Windows app", "Always installed")
        row = tk.Frame(app.body, bg=SURFACE)
        row.pack(fill="x", pady=(8, 4))
        Field(row, o["target"], width=20, bg=SURFACE).pack(side="left", fill="x", expand=True)
        Button(row, "Browse", lambda: self._browse(o["target"]), "tonal", 100, 42, SURFACE).pack(side="left", padx=(8, 0))
        for text, var in (("Start menu shortcut", o["start_menu"]), ("Desktop shortcut", o["desktop"]),
                          ("Virtual controller driver (ViGEmBus)", o["driver"])):
            self._switch_row(app.body, text, var)

        linux = GlassCard(left, 472, radius=24, bg=BG)
        linux.pack(fill="x")
        self._section(linux.body, "Linux (Ubuntu and Arch)", "Copies the files for another PC")
        self._switch_row(linux.body, "Copy the Linux packages to a folder", o["linux"])
        row = tk.Frame(linux.body, bg=SURFACE)
        row.pack(fill="x", pady=(8, 0))
        Field(row, o["linux_dir"], width=20, bg=SURFACE).pack(side="left", fill="x", expand=True)
        Button(row, "Browse", lambda: self._browse(o["linux_dir"]), "tonal", 100, 42, SURFACE).pack(side="left", padx=(8, 0))

        deck = GlassCard(right, 472, radius=24, bg=BG)
        deck.pack(fill="x", pady=(0, 14))
        self._section(deck.body, "Steam Deck plugin", "Needs Decky Loader and SSH on the Deck")
        self._switch_row(deck.body, "Install the plugin on the Steam Deck", o["deck"])
        row = tk.Frame(deck.body, bg=SURFACE)
        row.pack(fill="x", pady=(8, 2))
        tk.Label(row, text="Deck address (IP or name)", font=(FONT, 10), fg=MUTED, bg=SURFACE).pack(anchor="w")
        Field(row, o["deck_host"], width=40, bg=SURFACE).pack(fill="x", pady=(4, 0))
        self._switch_row(deck.body, "Open the install window now", o["open_deck"], helper=(
            "A console opens for the Deck's passwords. This window never sees them."))
        self._switch_row(deck.body, "Automatic updates on the Deck", o["deck_auto"], helper=(
            "Off. On: the Deck installs new releases by itself."))

        updates = GlassCard(right, 472, radius=24, bg=BG)
        updates.pack(fill="x")
        self._section(updates.body, "Updates", "Off on this PC")
        self._switch_row(updates.body, "Check for updates when the app starts", o["check_updates"], helper=(
            "On: it looks for a newer release at startup. It never installs one."))

    def _section(self, parent, title, note):
        tk.Label(parent, text=title, font=(DISPLAY, 14, "bold"), fg=TEXT, bg=SURFACE).pack(anchor="w")
        tk.Label(parent, text=note, font=(FONT, 9), fg=MUTED, bg=SURFACE).pack(anchor="w")

    def _switch_row(self, parent, text, variable, helper=None):
        row = tk.Frame(parent, bg=SURFACE)
        row.pack(fill="x", pady=(8, 0))
        Switch(row, variable, bg=SURFACE).pack(side="left", anchor="n")
        text_frame = tk.Frame(row, bg=SURFACE)
        text_frame.pack(side="left", padx=(12, 0))
        tk.Label(text_frame, text=text, font=(FONT, 11), fg=TEXT, bg=SURFACE).pack(anchor="w")
        if helper:
            tk.Label(text_frame, text=helper, font=(FONT, 9), fg=MUTED, bg=SURFACE, justify="left",
                     wraplength=330).pack(anchor="w")

    def _browse(self, variable):
        chosen = filedialog.askdirectory(initialdir=variable.get() or os.path.expanduser("~"))
        if chosen:
            variable.set(os.path.normpath(chosen))

    # ---- install -------------------------------------------------------------------------------------------
    def _build_install(self):
        page = tk.Frame(self.body, bg=BG)
        self.screens["install"] = page
        left = GlassCard(page, 430, radius=24, bg=BG)
        left.pack(side="left", fill="y", padx=(0, 16), anchor="n")
        self._section(left.body, "Installing", "Keep this window open")
        self.progress = Progress(left.body, width=360, height=12, bg=SURFACE)
        self.progress.pack(anchor="w", pady=(12, 6))
        self.percent = tk.Label(left.body, text="", font=(DISPLAY, 11, "bold"), fg=PRIMARY, bg=SURFACE)
        self.percent.pack(anchor="w")
        for key, title in STEPS:
            row = tk.Frame(left.body, bg=SURFACE)
            row.pack(fill="x", pady=3)
            glyph = tk.Label(row, text="○", font=("Segoe UI Symbol", 12), fg=OUTLINE, bg=SURFACE, width=2)
            glyph.pack(side="left")
            tk.Label(row, text=title, font=(FONT, 11), fg=MUTED, bg=SURFACE).pack(side="left")
            self.step_labels[key] = glyph
        right = GlassCard(page, 520, radius=24, bg=BG)
        right.pack(side="left", fill="both", expand=True, anchor="n")
        self._section(right.body, "Details", "What the setup is doing")
        self.log_text = tk.Text(right.body, width=56, height=22, bg=FIELD, fg=TEXT, relief="flat", bd=0,
                                font=(MONO, 9), wrap="word", highlightthickness=0)
        self.log_text.pack(fill="both", expand=True, pady=(8, 0))
        self.log_text.configure(state="disabled")

    def _start_install(self):
        o = self.options
        if o["deck"].get() and not valid_host(o["deck_host"].get().strip()):
            self._warn("Enter the Steam Deck's address first (its IP address, or a name such as steamdeck.local).")
            return
        if o["linux"].get() and not o["linux_dir"].get().strip():
            self._warn("Choose the folder for the Linux files.")
            return
        if not o["target"].get().strip():
            self._warn("Choose the folder for the Windows app.")
            return
        options = {
            "target": o["target"].get().strip(),
            "start_menu": o["start_menu"].get(),
            "desktop": o["desktop"].get(),
            "register": o["register"].get(),
            "driver": o["driver"].get(),
            "deck_host": o["deck_host"].get().strip() if o["deck"].get() else "",
            "deck_auto_updates": o["deck_auto"].get(),
            "open_deck": o["open_deck"].get(),
            "linux_dir": o["linux_dir"].get().strip() if o["linux"].get() else "",
            "check_updates": o["check_updates"].get(),
        }
        for glyph in self.step_labels.values():
            glyph.configure(text="○", fg=OUTLINE)
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.progress.set(0)
        self.show("install")
        self.progress.start_moving()
        threading.Thread(target=self._worker, args=(options,), daemon=True).start()

    def _worker(self, options):
        def log(text):
            self.queue.put(("log", write_log(text)))

        def progress(fraction):
            self.queue.put(("progress", fraction))

        def step(key, status):
            self.queue.put(("step", key, status))

        try:
            failures = install_all(options, log, progress, step)
        except Exception as exc:  # the window must always get an answer
            failures = [f"the setup stopped: {exc}"]
        self.queue.put(("finished", failures))

    def _pump(self):
        try:
            while True:
                message = self.queue.get_nowait()
                kind = message[0]
                if kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", message[1] + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "progress":
                    self.progress.set(message[1])
                    self.percent.configure(text=f"{round(message[1] * 100)}%")
                elif kind == "step":
                    _, key, status = message
                    symbol, color = {"running": ("●", PRIMARY), "done": ("✓", GOOD),
                                     "failed": ("✕", ERROR), "skipped": ("–", OUTLINE)}[status]
                    self.step_labels[key].configure(text=symbol, fg=color)
                elif kind == "finished":
                    self.failures = message[1]
                    self.progress.stop()
                    self.progress.set(1.0)
                    self.percent.configure(text="100%")
                    self.installed = installed_copy()
                    self._fill_done()
                    self.show("done")
        except queue.Empty:
            pass
        self.after(40, self._pump)

    # ---- done ----------------------------------------------------------------------------------------------
    def _build_done(self):
        page = tk.Frame(self.body, bg=BG)
        self.screens["done"] = page
        card = GlassCard(page, 1000, radius=26, bg=BG)
        card.pack(fill="x", anchor="n")
        self.done_glyph = tk.Canvas(card.body, width=96, height=96, bg=SURFACE, highlightthickness=0, bd=0)
        self.done_glyph.pack(anchor="w")
        self.done_title = tk.Label(card.body, text="", font=(DISPLAY, 24, "bold"), fg=TEXT, bg=SURFACE)
        self.done_title.pack(anchor="w", pady=(8, 8))
        self.done_text = tk.Label(card.body, text="", font=(FONT, 11), fg=MUTED, bg=SURFACE, justify="left",
                                  wraplength=900)
        self.done_text.pack(anchor="w")

    def _fill_done(self):
        o = self.options
        ok = not self.failures
        self.done_glyph.delete("all")
        self.done_glyph.create_oval(4, 4, 92, 92, fill=PRIMARY if ok else ERROR, outline="")
        if ok:
            self.done_glyph.create_line(28, 50, 42, 64, 70, 36, fill=ON_PRIMARY, width=7, capstyle="round", joinstyle="round")
        else:
            self.done_glyph.create_line(34, 34, 62, 62, fill=ERROR_CONTAINER, width=7, capstyle="round")
            self.done_glyph.create_line(62, 34, 34, 62, fill=ERROR_CONTAINER, width=7, capstyle="round")
        self.done_title.configure(text="Joy-Con 2 Studio is installed" if ok else "The setup finished with problems")
        lines = []
        if self.failures:
            lines += ["What went wrong:"] + [f"   {text}" for text in self.failures]
            lines.append(f"The log is at {LOG_FILE}")
        else:
            lines.append(f"The Windows app is in {o['target'].get()}.")
        if o["deck"].get() and o["deck_host"].get().strip():
            lines.append("Steam Deck: finish the install in the console window that opened.")
        if o["linux"].get():
            lines.append(f"Linux: the files are in {o['linux_dir'].get()}. Run install-linux.sh there.")
        self.done_text.configure(text="\n".join(lines))

    # ---- uninstall -----------------------------------------------------------------------------------------
    def _build_uninstall(self):
        page = tk.Frame(self.body, bg=BG)
        self.screens["uninstall"] = page
        card = GlassCard(page, 1000, radius=26, bg=BG)
        card.pack(fill="x", anchor="n")
        folder = self.installed[0] if self.installed else default_install_dir()
        tk.Label(card.body, text="Remove Joy-Con 2 Studio?", font=(DISPLAY, 24, "bold"), fg=TEXT, bg=SURFACE).pack(anchor="w")
        tk.Label(card.body, text=("This removes the app from:\n" + folder + "\n\nIt also removes the shortcuts and the "
                                  "entry in Settings > Apps. Your settings go with it."),
                 font=(FONT, 11), fg=MUTED, bg=SURFACE, justify="left", wraplength=900).pack(anchor="w", pady=(10, 0))

    def _start_uninstall(self):
        if not self.installed:
            self._warn("Joy-Con 2 Studio is not installed.")
            return
        target = self.installed[0]
        try:
            failures = uninstall_copy(target, write_log)
        except RuntimeError as exc:
            self._warn(str(exc))
            return
        self.failures = failures
        self.installed = None
        self._fill_done()
        self.done_title.configure(text="Joy-Con 2 Studio is being removed")
        self.done_text.configure(text=f"The files in {target} are removed once this window closes.")
        self.show("done")

    def _warn(self, text):
        write_log(text)
        window = tk.Toplevel(self, bg=BG)
        window.title("Joy-Con 2 Studio Setup")
        window.transient(self)
        card = GlassCard(window, 540, radius=22, bg=BG)
        card.pack(padx=16, pady=16)
        tk.Label(card.body, text=text, font=(FONT, 11), fg=TEXT, bg=SURFACE, justify="left", wraplength=480).pack(anchor="w")
        Button(card.body, "OK", window.destroy, "tonal", 110, 40, SURFACE).pack(anchor="e", pady=(14, 0))


# ---- command line -----------------------------------------------------------------------------------------------
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Joy-Con 2 Studio setup for Windows.")
    parser.add_argument("--silent", action="store_true", help="install without a window")
    parser.add_argument("--uninstall", action="store_true", help="remove the app (with --silent: without a window)")
    parser.add_argument("--dir", help="folder for the Windows app (default: %%LOCALAPPDATA%%\\Programs\\Joy-Con 2 Studio)")
    parser.add_argument("--no-shortcuts", action="store_true", help="no Start menu shortcut")
    parser.add_argument("--desktop-shortcut", action="store_true", help="add a desktop shortcut")
    parser.add_argument("--no-driver", action="store_true", help="do not install the virtual controller driver")
    parser.add_argument("--no-register", action="store_true", help="do not add the app to Settings > Apps")
    parser.add_argument("--deck", metavar="HOST", help="prepare the Steam Deck install for this address")
    parser.add_argument("--deck-auto-updates", choices=("on", "off"), default="off")
    parser.add_argument("--open-deck-window", action="store_true", help="open the Steam Deck console (silent mode)")
    parser.add_argument("--linux-dir", help="copy the Linux files to this folder")
    parser.add_argument("--check-updates", choices=("on", "off"), default="off")
    return parser.parse_args(argv)


def run_silent(args) -> int:
    if args.uninstall:
        target = args.dir or (installed_copy() or (None,))[0]
        if not target:
            write_log("Nothing to uninstall: no folder was given and no copy is installed.")
            return 1
        try:
            failures = uninstall_copy(target, write_log)
        except RuntimeError as exc:
            write_log(str(exc))
            return 1
        return 1 if failures else 0
    options = {
        "target": args.dir or default_install_dir(),
        "start_menu": not args.no_shortcuts,
        "desktop": args.desktop_shortcut,
        "register": not args.no_register,
        "driver": not args.no_driver,
        "deck_host": args.deck or "",
        "deck_auto_updates": args.deck_auto_updates == "on",
        "open_deck": args.open_deck_window,
        "linux_dir": args.linux_dir or "",
        "check_updates": args.check_updates == "on",
    }
    failures = install_all(options, write_log, None, lambda key, status: write_log(f"{key}: {status}"))
    for text in failures:
        write_log("FAILED " + text)
    return 1 if failures else 0


def make_dpi_aware() -> None:
    """Draws at the screen's real resolution, so text and shapes stay sharp on scaled displays."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except (OSError, AttributeError):
        pass


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        os.remove(LOG_FILE)
    except OSError:
        pass
    write_log("Joy-Con 2 Studio setup")
    if args.silent:
        return run_silent(args)
    make_dpi_aware()
    window = SetupWindow(uninstall_only=args.uninstall)
    window.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
