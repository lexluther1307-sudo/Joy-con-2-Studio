# Joy-Con 2 Studio (Decky Loader plugin)

Shows Joy-Con 2 controllers to Steam on the Steam Deck. The Joy-Cons stay connected to the Windows PC. The PC sends their state to this plugin over Wi-Fi, and the plugin presents it to Steam as a virtual DualShock 4. Steam's rumble goes back to the PC and plays on the Joy-Cons.

Version 0.2.0. The plugin uses only the network. It does not use Bluetooth.

## Install on the Steam Deck

1. Install Decky Loader first.
2. Put this folder on the Deck, or unzip the release zip (it contains a `joycon2studio` folder).
3. In Konsole (Desktop Mode), go into that folder and run `bash install.sh`. It asks for your sudo password, installs the plugin to `~/homebrew/plugins/joycon2studio` and restarts the plugin loader.
4. Open the Quick Access menu and pick Joy-Con 2 Studio.

## Use it

1. On the PC, run the Windows app, connect the Joy-Cons, tick "Send controller to a Deck (network bridge)" and type this Deck's address. The plugin shows the address under "This Deck".
2. On the Deck, press **Scan network**. It listens for 5 seconds and shows the PC when the PC is sending.
3. Steam shows the controller as a Wireless Controller. Its Controller Test should respond to the sticks and buttons.

The bridge uses UDP port 26761. It is not encrypted or authenticated, so use it only on a network you trust. If the PC stops sending for half a second, the plugin shows the controller at rest until the PC sends again.

## If something fails

Open Konsole in Desktop Mode and run:

    journalctl -u plugin_loader -b --no-pager | tail -n 200

Send that output back, along with what the status line says.

## Known limits

- Steam sees a DualShock 4, not a Joy-Con.
- The Deck does not pair with the Joy-Cons over Bluetooth. Connect them to the PC.
- Gyro and accelerometer scaling are first guesses.
- Needs `/dev/uhid` (present on SteamOS). The plugin runs as root through the `root` flag in `plugin.json`.
