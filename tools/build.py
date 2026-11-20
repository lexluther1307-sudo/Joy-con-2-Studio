"""Build the Windows app and the Steam Deck plugin with one matching version number.

Each run compares the sources with the last build. If anything changed, the patch number goes up,
and the new version is written to joycon2_gui.py (APP_VERSION), decky/joycon2studio/main.py (VERSION)
and decky/joycon2studio/package.json. The app header, the Deck screen, the exe and the plugin zip
then all show the same number.

  py tools/build.py            bump if anything changed, then build the Deck bundle, the exe and the zip
  py tools/build.py --version  bump if anything changed, print the version, and stop

Outputs go to build/release/ (ignored by git), and the exe also replaces JoyCon2Studio.exe in the project
folder. tools/deck_deploy.py install runs the same version
step and the Deck bundle build before it uploads.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "decky" / "joycon2studio"
APP = ROOT / "joycon2_gui.py"
SPEC = ROOT / "JoyCon2Studio.spec"
STATE_FILE = ROOT / "build" / "version-state.json"
RELEASE_DIR = ROOT / "build" / "release"
PROJECT_EXE = ROOT / "JoyCon2Studio.exe"  # the copy in the project folder, the one you run
PLUGIN_FILES = ["plugin.json", "main.py", "joycon2_link.py", "uhid_pad.py", "updater.py", "package.json",
                "README.md", "install.sh", "dist/index.js"]
SOURCES = [
    APP,
    SPEC,
    ROOT / "README.md",
    ROOT / "linux_mouse.py",
    ROOT / "installer" / "joycon2studio_setup.py",
    ROOT / "installer" / "linux" / "install-linux.sh",
    ROOT / "packaging" / "linux" / "joycon2studio.desktop",
    ROOT / "packaging" / "linux" / "70-joycon2studio.rules",
    ROOT / "packaging" / "linux" / "joycon2studio.conf",
    PLUGIN / "joycon2_link.py",
    PLUGIN / "main.py",
    PLUGIN / "uhid_pad.py",
    PLUGIN / "updater.py",
    PLUGIN / "install.sh",
    PLUGIN / "plugin.json",
    PLUGIN / "package.json",
    PLUGIN / "README.md",
    PLUGIN / "src" / "index.tsx",
]
# The lines that carry the version: (pattern, replacement template). Used for both writing and hashing.
VERSION_LINES = {
    APP: (r'^APP_VERSION = "[^"]*"', 'APP_VERSION = "{v}"'),
    PLUGIN / "main.py": (r'^VERSION = "[^"]*"', 'VERSION = "{v}"'),
    PLUGIN / "package.json": (r'"version": "[^"]*"', '"version": "{v}"'),
}


def _normalize(path: Path, text: str) -> str:
    """The version number itself does not count as a change."""
    if path in VERSION_LINES:
        pattern, template = VERSION_LINES[path]
        return re.sub(pattern, template.format(v="?"), text, count=1, flags=re.M)
    return text


def source_hash() -> str:
    digest = hashlib.sha256()
    for path in SOURCES:
        digest.update(path.name.encode("utf-8"))
        digest.update(_normalize(path, path.read_text(encoding="utf-8")).encode("utf-8"))
    return digest.hexdigest()


def current_version() -> str:
    match = re.search(r'^APP_VERSION = "([^"]*)"', APP.read_text(encoding="utf-8"), re.M)
    if not match:
        sys.exit("APP_VERSION was not found in joycon2_gui.py")
    return match.group(1)


def bump(version: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def write_version(version: str):
    """Checks every version line first, then writes them all, so a failure changes nothing."""
    updates = []
    for path, (pattern, template) in VERSION_LINES.items():
        text = path.read_text(encoding="utf-8")
        new_text, count = re.subn(pattern, template.format(v=version), text, count=1, flags=re.M)
        if count != 1:
            sys.exit(f"could not find the version line in {path.name}; nothing was changed")
        updates.append((path, text, new_text))
    for path, old_text, new_text in updates:
        if new_text != old_text:
            path.write_text(new_text, encoding="utf-8", newline="\n")


def sync_versions() -> str:
    """Bumps the version if the sources changed since the last build, and writes it everywhere."""
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    version = current_version()
    if source_hash() != state.get("hash"):
        version = bump(version)
        print(f"Sources changed: version is now {version}.")
    write_version(version)
    digest = source_hash()  # the version lines are ignored by the hash, so this matches the next run
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"version": version, "hash": digest}, indent=2) + "\n", encoding="utf-8")
    return version


def _run(command, cwd: Path):
    result = subprocess.run(command, cwd=cwd)
    if result.returncode != 0:
        sys.exit(f"command failed ({result.returncode}): {' '.join(map(str, command))}")


def build_plugin_bundle():
    """Rebuilds the Deck's screen (dist/index.js) from src/index.tsx."""
    npm = shutil.which("npm")
    if npm is None:
        sys.exit("npm was not found. Install Node.js, then run this again.")
    _run([npm, "run", "build"], PLUGIN)


def build_exe():
    _run([sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm"], ROOT)


def build_release(version: str) -> list:
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    exe = RELEASE_DIR / f"JoyCon2Studio-{version}.exe"
    shutil.copy2(ROOT / "dist" / "JoyCon2Studio.exe", exe)
    zip_path = RELEASE_DIR / f"joycon2studio-plugin-{version}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel in PLUGIN_FILES:
            archive.write(PLUGIN / rel, f"joycon2studio/{rel}")
        archive.write(ROOT / "LICENSE", "joycon2studio/LICENSE")
    lines = []
    for path in (exe, zip_path):
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
    (RELEASE_DIR / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [exe, zip_path]


def update_project_exe(exe: Path, version: str) -> bool:
    """Copies the new exe over JoyCon2Studio.exe in the project folder. Windows refuses while the app is open."""
    try:
        shutil.copy2(exe, PROJECT_EXE)
    except PermissionError:
        print(f"Could not update {PROJECT_EXE.name} in the project folder: it is open. Close the app and run this again.")
        return False
    print(f"  {PROJECT_EXE.name} in the project folder is now {version}.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Windows app and the Deck plugin with one version.")
    parser.add_argument("--version", action="store_true", help="only update the version and print it")
    args = parser.parse_args()
    version = sync_versions()
    if args.version:
        print(version)
        return 0
    build_plugin_bundle()
    build_exe()
    released = build_release(version)
    for path in released:
        print(f"  build/release/{path.name}  ({path.stat().st_size} bytes)")
    updated = update_project_exe(released[0], version)
    print(f"Built version {version}. The Windows app, the Deck screen and the plugin all show {version}.")
    return 0 if updated else 1


if __name__ == "__main__":
    sys.exit(main())
