"""Builds the Windows setup program, JoyCon2Studio-Setup-<version>.exe, with everything it installs inside.

Run the other builds first, so that all the files have the same version:

  py tools/build.py                 the Windows app (exe) and the Steam Deck plugin (zip)
  py tools/build_linux.py           the Ubuntu .deb and the Arch Linux package
  py tools/build_installer.py       the setup program, with those files inside

Output: build/release/JoyCon2Studio-Setup-<version>.exe. Its line is added to build/release/SHA256SUMS.txt.
"""

import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / "build" / "release"
LINUX = ROOT / "build" / "linux"
INSTALLER = ROOT / "installer"
PAYLOAD = ROOT / "build" / "installer" / "payload"
SETUP_SCRIPT = INSTALLER / "joycon2studio_setup.py"
LINUX_SCRIPT = INSTALLER / "linux" / "install-linux.sh"


def app_version() -> str:
    match = re.search(r'^APP_VERSION = "([^"]*)"', (ROOT / "joycon2_gui.py").read_text(encoding="utf-8"), re.M)
    if not match:
        sys.exit("APP_VERSION was not found in joycon2_gui.py")
    return match.group(1)


def need(path: Path) -> Path:
    if not path.exists():
        sys.exit(f"{path} is missing. Run tools/build.py and tools/build_linux.py for this version first.")
    return path


def prepare_payload(version: str) -> None:
    shutil.rmtree(PAYLOAD, ignore_errors=True)
    (PAYLOAD / "linux").mkdir(parents=True)
    shutil.copy2(need(RELEASE / f"JoyCon2Studio-{version}.exe"), PAYLOAD / "JoyCon2Studio.exe")
    shutil.copy2(need(RELEASE / f"joycon2studio-plugin-{version}.zip"), PAYLOAD / "joycon2studio-plugin.zip")
    shutil.copy2(ROOT / "LICENSE", PAYLOAD / "LICENSE")
    (PAYLOAD / "version.txt").write_text(version + "\n", encoding="utf-8")
    linux = PAYLOAD / "linux"
    shutil.copy2(need(LINUX / f"joycon2studio_{version}_all.deb"), linux)
    shutil.copy2(need(LINUX / "arch" / f"joycon2studio-{version}.tar.gz"), linux)
    shutil.copy2(need(LINUX / "arch" / "PKGBUILD"), linux)
    # The Linux script must keep LF line endings, or bash on Linux cannot run it
    script = LINUX_SCRIPT.read_bytes().replace(b"\r\n", b"\n")
    (linux / "install-linux.sh").write_bytes(script)


def build(version: str) -> Path:
    name = f"JoyCon2Studio-Setup-{version}"
    work = ROOT / "build" / "installer"
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
        "--name", name,
        "--add-data", f"{PAYLOAD};payload",
        "--workpath", str(ROOT / "build" / "installer-work"),
        "--specpath", str(work),
        "--distpath", str(work / "dist"),
        str(SETUP_SCRIPT),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    built = work / "dist" / f"{name}.exe"
    RELEASE.mkdir(parents=True, exist_ok=True)
    target = RELEASE / f"{name}.exe"
    shutil.copy2(built, target)
    return target


def add_checksum(path: Path) -> None:
    sums = RELEASE / "SHA256SUMS.txt"
    lines = []
    if sums.exists():
        lines = [line for line in sums.read_text(encoding="utf-8").splitlines()
                 if line.strip() and line.split()[-1] != path.name]
    lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    version = app_version()
    prepare_payload(version)
    exe = build(version)
    add_checksum(exe)
    print(f"Setup:  {exe}  ({exe.stat().st_size} bytes)")
    print(f"Payload: {PAYLOAD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
