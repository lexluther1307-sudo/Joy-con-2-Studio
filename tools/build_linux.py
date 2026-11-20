"""Build the Linux packages: an Arch Linux PKGBUILD with its source tarball, and an Ubuntu .deb.

Both packages install the same files: the app and its helper modules in /usr/lib/joycon2studio, a launcher at
/usr/bin/joycon2studio, a desktop entry, a udev rule that lets the logged-in user open /dev/uhid and /dev/uinput,
and a modules-load file for the uhid and uinput kernel modules.

  py tools/build_linux.py     writes build/linux/arch/ (PKGBUILD and the source tarball)
                              and build/linux/joycon2studio_<version>_all.deb

Install on Arch Linux:  cd build/linux/arch && makepkg -si
Install on Ubuntu:      sudo apt install ./build/linux/joycon2studio_<version>_all.deb
Run tools/build.py first, so that the version matches the Windows app and the Deck plugin.
"""

import hashlib
import io
import re
import shutil
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "decky" / "joycon2studio"
PACKAGING = ROOT / "packaging" / "linux"
OUT = ROOT / "build" / "linux"
PACKAGE = "joycon2studio"
HOMEPAGE = "https://github.com/lexluther1307-sudo/Joy-con-2-Studio"
MAINTAINER = "lexluther1307-sudo <lexluther1307-sudo@users.noreply.github.com>"
DEB_DEPENDS = "python3 (>= 3.10), python3-tk, python3-bleak, bluez"
ARCH_DEPENDS = ("python", "python-bleak", "tk", "bluez")
SUMMARY = "Use Nintendo Switch 2 Joy-Con 2 controllers on Linux as a gamepad and mouse"
DETAIL = (
    " Shows the Joy-Cons to Steam and games as a virtual DualShock 4 (through /dev/uhid), and can use one",
    " Joy-Con as a mouse (through /dev/uinput). Experimental: not yet tested on Linux hardware.",
)
LAUNCHER = (
    "#!/bin/sh\n"
    "# Starts Joy-Con 2 Studio. Needs Python 3 with tkinter.\n"
    'exec python3 /usr/lib/joycon2studio/joycon2_gui.py "$@"\n'
)
# (source file, where it goes inside the package)
INSTALLED_FILES = [
    (ROOT / "joycon2_gui.py", "usr/lib/joycon2studio/joycon2_gui.py"),
    (ROOT / "linux_mouse.py", "usr/lib/joycon2studio/linux_mouse.py"),
    (PLUGIN / "joycon2_link.py", "usr/lib/joycon2studio/joycon2_link.py"),
    (PLUGIN / "uhid_pad.py", "usr/lib/joycon2studio/uhid_pad.py"),
    (ROOT / "LICENSE", "usr/share/licenses/joycon2studio/LICENSE"),
    (PACKAGING / "joycon2studio.desktop", "usr/share/applications/joycon2studio.desktop"),
    (PACKAGING / "70-joycon2studio.rules", "usr/lib/udev/rules.d/70-joycon2studio.rules"),
    (PACKAGING / "joycon2studio.conf", "usr/lib/modules-load.d/joycon2studio.conf"),
]


def app_version() -> str:
    text = (ROOT / "joycon2_gui.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION = "([^"]*)"', text, re.M)
    if not match:
        sys.exit("APP_VERSION was not found in joycon2_gui.py")
    return match.group(1)


def package_files() -> list:
    """(path inside the package, contents, file mode) for every file the packages install."""
    files = []
    for source, target in INSTALLED_FILES:
        data = source.read_bytes().replace(b"\r\n", b"\n")  # text files as Linux expects them
        files.append((target, data, 0o644))
    files.append(("usr/bin/joycon2studio", LAUNCHER.encode("ascii"), 0o755))
    return files


def directories_of(names) -> list:
    """Every folder above the files, parents first. dpkg needs these entries: it does not create the folders
    for the files it unpacks, so without them the package fails with 'No such file or directory'."""
    folders = set()
    for name in names:
        parts = name.split("/")
        for depth in range(1, len(parts)):
            folders.add("/".join(parts[:depth]))
    return sorted(folders, key=lambda folder: (folder.count("/"), folder))


def tar_gz(entries, prefix: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
        for folder in directories_of(name for name, _, _ in entries):
            info = tarfile.TarInfo(prefix + folder + "/")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            archive.addfile(info)
        for name, data, mode in entries:
            info = tarfile.TarInfo(prefix + name)
            info.size = len(data)
            info.mode = mode
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def ar_archive(members) -> bytes:
    """A .deb is an ar archive. Each member has a 60-byte header, and data is padded to an even length."""
    out = bytearray(b"!<arch>\n")
    for name, data in members:
        header = f"{name:<16}{0:<12}{0:<6}{0:<6}{0o100644:<8o}{len(data):<10}"
        out += header.encode("ascii") + b"`\n" + data
        if len(data) % 2:
            out += b"\n"
    return bytes(out)


def write_deb(path: Path, version: str, files) -> None:
    installed_kb = (sum(len(data) for _, data, _ in files) + 1023) // 1024
    control = "\n".join([
        f"Package: {PACKAGE}",
        f"Version: {version}",
        "Architecture: all",
        f"Maintainer: {MAINTAINER}",
        f"Installed-Size: {installed_kb}",
        f"Depends: {DEB_DEPENDS}",
        "Section: games",
        "Priority: optional",
        f"Homepage: {HOMEPAGE}",
        f"Description: {SUMMARY}",
        *DETAIL,
    ]) + "\n"
    path.write_bytes(ar_archive([
        ("debian-binary", b"2.0\n"),
        ("control.tar.gz", tar_gz([("control", control.encode("utf-8"), 0o644)], "./")),
        ("data.tar.gz", tar_gz(files, "./")),
    ]))


def write_arch(version: str, files) -> Path:
    folder = OUT / "arch"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    top = f"{PACKAGE}-{version}"
    tarball = folder / f"{top}.tar.gz"
    tarball.write_bytes(tar_gz(files, f"{top}/"))
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
    depends = " ".join(f"'{name}'" for name in ARCH_DEPENDS)
    pkgbuild = f"""# Maintainer: lexluther1307-sudo
pkgname={PACKAGE}
pkgver={version}
pkgrel=1
pkgdesc='{SUMMARY}'
arch=('any')
url='{HOMEPAGE}'
license=('MIT')
depends=({depends})
source=("${{pkgname}}-${{pkgver}}.tar.gz")
sha256sums=('{digest}')

package() {{
  cp -a "${{srcdir}}/${{pkgname}}-${{pkgver}}/usr" "${{pkgdir}}/"
}}
"""
    (folder / "PKGBUILD").write_text(pkgbuild, encoding="utf-8", newline="\n")
    return folder


def add_checksums(*paths: Path) -> None:
    """Adds the packages to build/release/SHA256SUMS.txt, the checksum file of the release, so one file covers all."""
    sums = ROOT / "build" / "release" / "SHA256SUMS.txt"
    sums.parent.mkdir(parents=True, exist_ok=True)
    names = {path.name for path in paths}
    lines = []
    if sums.exists():
        lines = [line for line in sums.read_text(encoding="utf-8").splitlines()
                 if line.strip() and line.split()[-1] not in names]
    lines += [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in paths]
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    version = app_version()
    files = package_files()
    OUT.mkdir(parents=True, exist_ok=True)
    folder = write_arch(version, files)
    tarball = folder / f"{PACKAGE}-{version}.tar.gz"
    deb = OUT / f"{PACKAGE}_{version}_all.deb"
    write_deb(deb, version, files)
    add_checksums(deb, tarball)
    print(f"Arch Linux: {folder / 'PKGBUILD'} with {tarball.name}")
    print(f"Ubuntu:     {deb}  ({deb.stat().st_size} bytes)")
    print("Checksums:  build/release/SHA256SUMS.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
