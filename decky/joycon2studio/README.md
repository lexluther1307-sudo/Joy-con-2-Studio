# Joy-Con 2 Studio (Decky Loader plugin)

Connects Joy-Con 2 controllers to the Steam Deck and presents them to Steam as a virtual
DualShock 4: buttons, sticks, gyro, accelerometer and rumble. Steam's rumble plays on the Joy-Cons.

Status: the network bridge (below) was tested on a Deck. Direct Joy-Con pairing on the Deck does not work yet, so use the bridge. Send back logs if something fails.

## Install on the Steam Deck

1. Install Decky Loader first.
2. Copy this folder to `~/homebrew/plugins/` (the folder must be named `joycon2studio`).
3. Reboot the Deck.
4. Open the Quick Access menu, pick Joy-Con 2 Studio.

## Test (direct Joy-Con connection)

1. The status line should say "Virtual DualShock 4: ready".
2. Hold SYNC on each Joy-Con, then press "Scan for Joy-Cons". Each side should show connected with a battery level.
3. Press "Test rumble (right)" and "(left)". Try a song.
4. In Steam (Settings > Controller), check that the "Wireless Controller" shows up. Its Controller Test should respond to the sticks and gyro.
5. Steam's rumble test or a game with rumble should buzz the Joy-Cons.

## Network bridge (Joy-Cons stay on the PC)

Direct pairing on the Deck fails, so the PC keeps the Joy-Cons and streams their state here over
UDP port 26761. The Deck's firewall allows that port by default.

1. Install the plugin as above and reboot the Deck.
2. On the PC, run `JoyCon2Studio.exe`, connect the Joy-Cons, tick "Send controller to a Deck (network bridge)" and type the Deck's IP.
3. The Deck shows the same virtual DualShock 4 in Steam. Steam's rumble goes back to the PC and buzzes the Joy-Cons.

If the PC stops sending for half a second, the Deck falls back to its own Joy-Cons.

## If something fails

Open Konsole in Desktop Mode and run:

    journalctl -u plugin_loader -b --no-pager | tail -n 200

Send that output back, along with what the status line says.

## Known limits

- Steam sees a DualShock 4, not a Joy-Con. Making Steam list the devices as Joy-Cons is a separate job.
- The mouse feature is not included.
- Gyro scale is a first guess. Tune the calibration values in `main.py` if the gyro runs too fast or slow.
- Needs `/dev/uhid` (present on SteamOS). The plugin runs as root through the `root` flag in `plugin.json`.
- The Bluetooth parts assume the Deck's Python is 3.11 (the bundled `py_modules` are cp311 Linux builds).
