"""Updates the Steam Deck plugin from the project's latest GitHub release. The plugin runs as root under Decky Loader.

About every six hours, and when Check for updates is pressed, the plugin asks GitHub for the latest release.
A newer release is installed only when no PC is connected, because the update restarts the plugin. The steps:
download joycon2studio-plugin-<version>.zip over HTTPS, check it against SHA256SUMS.txt from the same release,
unpack it into a folder in the settings folder, check that its Python files compile and that it declares the
release's version, then put it in place of the installed plugin. The old plugin is kept as a backup. Last,
plugin_loader is restarted so that the new code runs.

The checksum shows that the download is complete and matches the release. It does not protect against a
release that someone changed on purpose.
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
import zipfile

REPO = "lexluther1307-sudo/Joy-con-2-Studio"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
PLUGIN = "joycon2studio"
SUMS_NAME = "SHA256SUMS.txt"
REQUIRED_FILES = ("plugin.json", "main.py", "joycon2_link.py", "dist/index.js")
FIRST_CHECK_SECONDS = 120  # after the plugin starts
CHECK_EVERY_SECONDS = 6 * 3600
RETRY_WHEN_BUSY_SECONDS = 600  # a PC is connected: try again in ten minutes
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_UNPACKED_BYTES = 60 * 1024 * 1024
TIMEOUT_SECONDS = 30


class UpdateError(Exception):
    """A problem the user can read in the plugin's Updates section."""


def _describe(exc: BaseException) -> str:
    if isinstance(exc, UpdateError):
        return str(exc)
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def parse_version(text) -> tuple:
    match = re.fullmatch(r"\s*v?(\d+)\.(\d+)\.(\d+)\s*", str(text or ""))
    if match is None:
        raise UpdateError(f"not a version number: {text!r}")
    return tuple(int(part) for part in match.groups())


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def _request(url: str, accept: str):
    # Only the project's own GitHub addresses. Redirects from GitHub to its file servers are followed by urllib.
    if not url.startswith(("https://api.github.com/", DOWNLOAD_PREFIX)):
        raise UpdateError("refusing an address outside the project's GitHub releases")
    return urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "joycon2studio-plugin"})


def latest_release() -> dict:
    """The newest GitHub release: its version, and the addresses of the plugin zip and the checksum file."""
    with urllib.request.urlopen(_request(API_URL, "application/vnd.github+json"), timeout=TIMEOUT_SECONDS) as reply:
        data = json.loads(reply.read(1024 * 1024).decode("utf-8"))
    version = ".".join(str(part) for part in parse_version(data.get("tag_name")))
    assets = {asset.get("name"): asset.get("browser_download_url", "") for asset in data.get("assets", [])}
    zip_name = f"{PLUGIN}-plugin-{version}.zip"
    if zip_name not in assets or SUMS_NAME not in assets:
        raise UpdateError(f"release {version} has no {zip_name} and {SUMS_NAME}")
    if not all(assets[name].startswith(DOWNLOAD_PREFIX) for name in (zip_name, SUMS_NAME)):
        raise UpdateError("the release files are not on the project's GitHub releases")
    return {"version": version, "zip_name": zip_name, "zip_url": assets[zip_name], "sums_url": assets[SUMS_NAME]}


def download(url: str, dest: str, limit: int):
    with urllib.request.urlopen(_request(url, "application/octet-stream"), timeout=TIMEOUT_SECONDS) as reply:
        with open(dest, "wb") as out:
            total = 0
            while True:
                chunk = reply.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise UpdateError("the download is larger than expected")
                out.write(chunk)


def fetch_sums(url: str) -> str:
    with urllib.request.urlopen(_request(url, "text/plain"), timeout=TIMEOUT_SECONDS) as reply:
        return reply.read(64 * 1024).decode("utf-8", errors="replace")


def expected_sha256(sums_text: str, name: str):
    for line in sums_text.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+?)\s*", line)
        if match and match.group(2) == name:
            return match.group(1).lower()
    return None


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unpack(zip_path: str, dest: str):
    """Writes the plugin files into dest. Only files under the joycon2studio/ folder are accepted: no links,
    and no names that would leave dest."""
    written = 0
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            parts = info.filename.split("/")
            if (len(parts) < 2 or parts[0] != PLUGIN or "\\" in info.filename
                    or any(part in ("", ".", "..") for part in parts[1:])):
                raise UpdateError(f"unexpected file name in the update: {info.filename}")
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise UpdateError(f"the update contains a link: {info.filename}")
            target = os.path.join(dest, *parts[1:])
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as out:
                while True:
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_UNPACKED_BYTES:
                        raise UpdateError("the update is larger than expected")
                    out.write(chunk)


def check_package(root: str, version: str):
    """The unpacked plugin must have its files, its Python must compile, and it must declare the release's version."""
    for rel in REQUIRED_FILES:
        if not os.path.isfile(os.path.join(root, *rel.split("/"))):
            raise UpdateError(f"the update has no {rel}")
    for name in sorted(os.listdir(root)):
        if name.endswith(".py"):
            with open(os.path.join(root, name), "rb") as handle:
                try:
                    compile(handle.read(), name, "exec")
                except SyntaxError as exc:
                    raise UpdateError(f"{name} in the update does not compile: {exc}") from exc
    with open(os.path.join(root, "main.py"), encoding="utf-8") as handle:
        match = re.search(r'^VERSION = "([^"]*)"', handle.read(), re.M)
    declared = match.group(1) if match else ""
    if declared != version:
        raise UpdateError(f"the update declares version {declared!r}, but the release is {version}")


def swap_in(staged: str, plugin_dir: str, backup: str):
    """Puts staged in place of plugin_dir and keeps the old plugin in backup, replacing any older backup."""
    if os.path.lexists(backup):
        shutil.rmtree(backup)
    had_old = os.path.isdir(plugin_dir)
    if had_old:
        shutil.move(plugin_dir, backup)
    try:
        shutil.move(staged, plugin_dir)
    except Exception:
        if os.path.lexists(plugin_dir):
            shutil.rmtree(plugin_dir, ignore_errors=True)
        if had_old:
            shutil.move(backup, plugin_dir)  # put the old plugin back
        raise


def restart_plugin_loader():
    """Restarts Decky's plugin loader, which runs this plugin. --no-block returns at once, so the restart happens
    after this plugin has finished the update and returned."""
    subprocess.Popen(["systemctl", "--no-block", "restart", "plugin_loader"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class UpdateManager:
    """Checks for a newer release, installs it when no PC is connected, and reports its state to the Deck screen."""

    def __init__(self, current: str, plugin_dir: str, settings_dir: str, can_update, log,
                 restart=restart_plugin_loader):
        self.current = current
        self.plugin_dir = plugin_dir
        self.settings_file = os.path.join(settings_dir, "update.json")
        self.work_dir = os.path.join(settings_dir, "update")
        self.can_update = can_update  # callable: True when no PC is connected
        self.log = log
        self.restart = restart
        self.auto = True
        self.latest = ""
        self.state = "waiting"
        self.message = "Checking GitHub a couple of minutes after the plugin starts."
        self.last_check = 0.0
        self._loop = None
        self._task = None
        self._lock = None
        self._load()

    def _load(self):
        try:
            with open(self.settings_file, encoding="utf-8") as handle:
                data = json.load(handle)
            self.auto = bool(data.get("auto_update", True))
            self.last_check = float(data.get("last_check", 0.0))
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.settings_file), exist_ok=True)
            tmp = self.settings_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"auto_update": self.auto, "last_check": self.last_check}, handle)
            os.replace(tmp, self.settings_file)
        except OSError as exc:
            self.log.error(f"could not save the update settings: {exc}")

    def status(self) -> dict:
        return {
            "enabled": self.auto,
            "current": self.current,
            "latest": self.latest,
            "state": self.state,
            "message": self.message,
            "last_check": int(self.last_check),
        }

    def set_auto(self, enabled: bool):
        self.auto = bool(enabled)
        self._save()
        self.message = ("Automatic updates are on." if self.auto
                        else "Automatic updates are off. Use Check for updates now to update by hand.")

    def start(self, loop):
        self._loop = loop
        self._lock = asyncio.Lock()
        self._task = loop.create_task(self._run())

    def stop(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def request_check(self):
        if self._loop is not None:
            self._loop.create_task(self.check())

    async def _run(self):
        wait = FIRST_CHECK_SECONDS
        while True:
            await asyncio.sleep(wait)
            wait = CHECK_EVERY_SECONDS
            if self.auto and await self.check() == "deferred":
                wait = RETRY_WHEN_BUSY_SECONDS

    async def check(self) -> str:
        """One check. Returns "done", "deferred" (a PC is connected), "busy" (a check is already running) or "error"."""
        if self._lock is None or self._lock.locked():
            return "busy"
        async with self._lock:
            self.state = "checking"
            self.message = "Checking GitHub for a newer release..."
            try:
                release = await asyncio.to_thread(latest_release)
            except Exception as exc:
                self.state = "error"
                self.message = f"Could not check for updates: {_describe(exc)}"
                self.log.error(self.message)
                return "error"
            self.last_check = time.time()
            self.latest = release["version"]
            self._save()
            if not is_newer(release["version"], self.current):
                self.state = "uptodate"
                self.message = f"Up to date: version {self.current}."
                return "done"
            if not self.can_update():
                self.state = "waiting"
                self.message = f"Version {release['version']} is ready. It installs when no PC is connected."
                return "deferred"
            self.state = "installing"
            self.message = f"Downloading version {release['version']}..."
            try:
                staged = await asyncio.to_thread(self._prepare, release)
            except Exception as exc:
                self.state = "error"
                self.message = f"Update to {release['version']} failed: {_describe(exc)}. The installed version was kept."
                self.log.error(self.message)
                return "error"
            if not self.can_update():
                # A PC connected while the download ran: wait for the next check instead of restarting now
                shutil.rmtree(staged, ignore_errors=True)
                self.state = "waiting"
                self.message = f"Version {release['version']} is ready. It installs when no PC is connected."
                return "deferred"
            try:
                await asyncio.to_thread(swap_in, staged, self.plugin_dir, os.path.join(self.work_dir, "backup"))
            except Exception as exc:
                self.state = "error"
                self.message = f"Update to {release['version']} failed: {_describe(exc)}. The installed version was kept."
                self.log.error(self.message)
                return "error"
            self.state = "restarting"
            self.message = f"Updated to version {release['version']}. Restarting the plugin..."
            self.log.info(self.message)
            self.restart()
            return "done"

    def _prepare(self, release: dict) -> str:
        """Downloads and checks the release into a staging folder. Returns the folder."""
        os.makedirs(self.work_dir, exist_ok=True)
        staged = os.path.join(self.work_dir, "staged")
        zip_path = os.path.join(self.work_dir, release["zip_name"])
        for leftover in (staged, zip_path):  # an earlier attempt that did not finish
            if os.path.isdir(leftover):
                shutil.rmtree(leftover, ignore_errors=True)
            elif os.path.lexists(leftover):
                os.remove(leftover)
        download(release["zip_url"], zip_path, MAX_DOWNLOAD_BYTES)
        try:
            expected = expected_sha256(fetch_sums(release["sums_url"]), release["zip_name"])
            if expected is None or sha256_of(zip_path) != expected:
                raise UpdateError(f"the download does not match {SUMS_NAME} in the release")
            os.makedirs(staged)
            unpack(zip_path, staged)
            check_package(staged, release["version"])
        except Exception:
            shutil.rmtree(staged, ignore_errors=True)
            raise
        finally:
            if os.path.lexists(zip_path):
                os.remove(zip_path)
        return staged
