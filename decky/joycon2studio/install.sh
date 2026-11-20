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

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user (deck), not as root. The script uses sudo itself." >&2
    exit 1
fi

for needed in plugin.json main.py dist py_modules; do
    if [ ! -e "$SRC/$needed" ]; then
        echo "Missing $needed next to this script. Run it from inside the joycon2studio folder." >&2
        exit 1
    fi
done

if [ ! -d "$PLUGINS_DIR" ]; then
    echo "Decky Loader is not installed (no $PLUGINS_DIR). Install Decky Loader first, then run this again." >&2
    exit 1
fi

echo "Installing $PLUGIN to $DEST. Enter your sudo password when asked."

if [ -e "$DEST" ]; then
    BACKUP="$HOME/joycon2studio-backup-$STAMP"
    sudo mv "$DEST" "$BACKUP"
    echo "Previous version moved to $BACKUP"
fi

sudo mkdir -p "$DEST"
for item in plugin.json main.py package.json README.md LICENSE dist py_modules; do
    if [ -e "$SRC/$item" ]; then
        sudo cp -r "$SRC/$item" "$DEST/"
    fi
done
sudo chown -R root:root "$DEST"

sudo systemctl restart plugin_loader
echo "Installed. Open the Quick Access menu and pick Joy-Con 2 Studio."
