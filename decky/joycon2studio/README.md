# Joy-Con 2 Studio (Decky Loader plugin)

Shows Joy-Con 2 controllers to Steam on the Steam Deck. The Joy-Cons stay connected to the PC (Windows or Linux). The PC sends their state to this plugin over an encrypted Wi-Fi link, and the plugin presents it to Steam as a virtual DualShock 4. Steam's rumble goes back to the PC and plays on the Joy-Cons.

The plugin uses only the network. It does not use Bluetooth.

## Install on the Steam Deck

1. Install Decky Loader first.
2. Put this folder on the Deck, or unzip the release zip (it contains a `joycon2studio` folder).
3. In Konsole (Desktop Mode), go into that folder and run `bash install.sh`. It asks for your sudo password, installs the plugin to `~/homebrew/plugins/joycon2studio` and restarts the plugin loader.
4. Open the Quick Access menu and pick Joy-Con 2 Studio. It shows a six-digit pairing code under Pairing.

## Updates

The plugin updates itself from the project's latest GitHub release.

- About every six hours, and when you press **Check for updates now**, it asks GitHub for the latest release.
- A newer release is installed only when no PC is connected, because the update restarts the plugin. While a PC is connected, the update waits.
- Before anything is replaced, the download is checked against `SHA256SUMS.txt` from the same release. The new files must compile and must declare the release's version.
- The previous version is kept in `~/homebrew/settings/joycon2studio/update/backup`. Then plugin_loader is restarted, so the new code runs.
- Turn automatic updates off with the button in the Updates section. **Check for updates now** still installs a newer release.

The first version with updates has to be installed by hand (step 3 above). Later updates happen by themselves. The checksum shows that a download is complete and matches the release; it does not protect against a release that someone changed on purpose.

## Connect

1. On the Deck, note the six-digit pairing code.
2. On the PC, in the Windows or Linux app, enter the Deck's address and the six-digit pairing code, then tick "Connect to a Deck (encrypted)".
3. The Deck shows "Connected to the PC at ...". While it is connected, the PC stops using the Joy-Cons, and Steam on the Deck has the controller.
4. Disconnect from either side with its Disconnect button. Press Connect on the Deck to accept the PC again. The Deck makes a new code after every disconnect, so read the new code before you connect again.

If the PC goes silent for 1.5 seconds, the Deck shows the controller at rest, and the PC goes back to using the Joy-Cons. Press **Scan network** on the Deck to see whether the PC is sending.

## Encryption

Pairing uses SPAKE2 over Ed25519, so a recorded handshake cannot be checked offline for the six digits. Ten wrong codes from one address within a minute block that address for a minute. After pairing, every packet between the PC and the Deck is encrypted and authenticated (HMAC-SHA256, Python standard library only) with keys made fresh for each connection. Altered and replayed packets are ignored. The code changes after every disconnect. Keep it private, and press **New pairing code** on the Deck if it leaks. The PC keeps the code in its settings file.

## If something fails

Open Konsole in Desktop Mode and run:

    journalctl -u plugin_loader -b --no-pager | tail -n 200

Send that output back, along with what the status line says.

## Known limits

- Steam sees a DualShock 4, not a Joy-Con.
- The Deck does not pair with the Joy-Cons over Bluetooth. Connect them to the PC.
- The encryption has not been audited. Each connection uses fresh keys, so a leaked pairing code does not expose recorded past traffic. The code also changes after every disconnect.
- Gyro and accelerometer scaling are first guesses.
- Needs `/dev/uhid` (present on SteamOS). The plugin runs as root through the `root` flag in `plugin.json`.
- Automatic updates need a published GitHub release that contains `joycon2studio-plugin-<version>.zip` and `SHA256SUMS.txt`.
