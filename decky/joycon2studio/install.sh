#!/bin/bash
# Installs the Joy-Con 2 Studio Decky plugin on a Steam Deck.
#
# Run it in Konsole (Desktop Mode) from inside the joycon2studio folder:
#   bash install.sh
# It asks for your sudo password. Decky Loader must already be installed.

set -euo pipefail

PLUGIN="joycon2studio"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGINS_DIR="$HOME/homebrew/plugins"
DEST="$PLUGINS_DIR/$PLUGIN"
STAMP="$(date +%Y%m%d-%H%M%S)"

step() { echo "==> $*"; }

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user (deck), not as root. The script uses sudo itself." >&2
    exit 1
fi

step "Checking the plugin files next to this script ($SRC)"
for needed in plugin.json main.py joycon2_link.py uhid_pad.py updater.py dist; do
    if [ ! -e "$SRC/$needed" ]; then
        echo "Missing $needed next to this script. Run it from inside the joycon2studio folder." >&2
        exit 1
    fi
done

step "Checking that Decky Loader is installed ($PLUGINS_DIR)"
if [ ! -d "$PLUGINS_DIR" ]; then
    echo "Decky Loader is not installed (no $PLUGINS_DIR). Install Decky Loader first, then run this again." >&2
    exit 1
fi

step "Installing $PLUGIN to $DEST (enter your sudo password when asked)"
if [ -e "$DEST" ]; then
    BACKUP="$HOME/joycon2studio-backup-$STAMP"
    step "Moving the older version to $BACKUP"
    sudo mv -v "$DEST" "$BACKUP"
fi

step "Copying the plugin files"
sudo mkdir -p "$DEST"
for item in plugin.json main.py joycon2_link.py uhid_pad.py updater.py package.json README.md LICENSE dist; do
    if [ -e "$SRC/$item" ]; then
        sudo cp -rv "$SRC/$item" "$DEST/"
    fi
done

step "Setting the owner of $DEST to root"
sudo chown -R root:root "$DEST"

step "Restarting plugin_loader"
sudo systemctl restart plugin_loader

step "Done. Open the Quick Access menu and pick Joy-Con 2 Studio."
