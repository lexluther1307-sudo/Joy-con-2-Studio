#!/usr/bin/env bash
# Joy-Con 2 Studio for Linux: installs the app and everything it needs, on Ubuntu 24.04 or newer, and on Arch Linux
# (and Arch-based systems such as EndeavourOS, Manjaro and CachyOS).
#
# Run it as your normal user, from the folder that holds the package files. It asks for your password when it needs
# sudo. Afterwards, log out and back in, so that the access rules for /dev/uhid and /dev/uinput apply.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user, not as root. The script uses sudo when it needs it." >&2
    exit 1
fi

if [ ! -r /etc/os-release ]; then
    echo "This Linux has no /etc/os-release, so the script cannot tell which distribution it is." >&2
    exit 1
fi
# shellcheck disable=SC1091
. /etc/os-release

echo "==> Joy-Con 2 Studio for ${PRETTY_NAME:-Linux}"

case "${ID:-}" in
    ubuntu)
        major="${VERSION_ID%%.*}"
        if [ "$major" -lt 24 ]; then
            echo "Ubuntu ${VERSION_ID} is too old. The app needs python3-bleak 0.21, which comes with Ubuntu 24.04 or newer." >&2
            exit 1
        fi
        shopt -s nullglob
        debs=("$HERE"/joycon2studio_*_all.deb)
        shopt -u nullglob
        if [ "${#debs[@]}" -eq 0 ]; then
            echo "The .deb package is not in $HERE." >&2
            exit 1
        fi
        echo "==> Updating the package list"
        sudo apt-get update
        echo "==> Installing ${debs[0]##*/} with its dependencies (python3-tk, python3-bleak, bluez)"
        sudo apt-get install -y "${debs[0]}"
        ;;
    arch|endeavouros|manjaro|cachyos)
        shopt -s nullglob
        tarballs=("$HERE"/joycon2studio-*.tar.gz)
        shopt -u nullglob
        if [ ! -f "$HERE/PKGBUILD" ] || [ "${#tarballs[@]}" -eq 0 ]; then
            echo "The PKGBUILD and the joycon2studio-*.tar.gz source file must be in $HERE." >&2
            exit 1
        fi
        echo "==> Installing the dependencies (python, python-bleak, tk, bluez)"
        sudo pacman -S --needed --noconfirm python python-bleak tk bluez
        build_dir="$(mktemp -d)"
        trap 'rm -rf "$build_dir"' EXIT
        cp "$HERE/PKGBUILD" "${tarballs[@]}" "$build_dir/"
        echo "==> Building and installing the package"
        (cd "$build_dir" && makepkg -si --needed --noconfirm)
        ;;
    *)
        echo "This script installs on Ubuntu 24.04 or newer, and on Arch Linux and Arch-based systems." >&2
        echo "On other distributions, install python3 with tkinter and bleak, then run the app with: python3 /usr/lib/joycon2studio/joycon2_gui.py" >&2
        exit 1
        ;;
esac

echo "==> Loading the kernel modules for the virtual controller and the virtual mouse"
sudo modprobe uhid || echo "    (uhid is not available: a restart may be needed)"
sudo modprobe uinput || echo "    (uinput is not available: a restart may be needed)"

echo
echo "Done. Log out and back in (or restart), so that the access rules for /dev/uhid and /dev/uinput apply."
echo "Start Joy-Con 2 Studio from the application menu, or run: joycon2studio"
