# Joy-Con 2 Studio

Use Nintendo Switch 2 Joy-Con 2 controllers on Windows and on a Steam Deck.

The Windows app connects the Joy-Cons over Bluetooth LE and presents them to Windows and Steam as a virtual DualShock 4: sticks, buttons, gyro, accelerometer and rumble. The Steam Deck plugin (Decky Loader) can take the controllers from the PC over the network, so the Joy-Cons can stay on the PC.

> **Experimental and unofficial.** Tested on Windows 11 with a left and a right Joy-Con 2. The Steam Deck plugin uses Wi-Fi only; it does not pair over Bluetooth. The network bridge to the Deck is new and has not been tested end to end with Steam.

## Download

[Joy-Con 2 Studio for Windows](https://github.com/lexluther1307-sudo/Joy-con-2-Studio/releases/latest): download `JoyCon2Studio.exe` from the latest release. It needs no Python.

- The virtual controller needs the ViGEmBus driver. If the app shows **MISSING ViGEmBus**, click **Install ViGEmBus driver**. It downloads the official installer, checks that it is signed by Nefarius, and runs it after you approve the Windows admin prompt. If it shows **restart suggested**, a restart finishes the driver setup. It is optional.
- The app checks GitHub for a newer release when it starts. If there is one, the button at the top right changes to **Get** and the new version.

Before you run it:
- Check its SHA-256 hash against the one in the release notes: `certutil -hashfile JoyCon2Studio.exe SHA256`.
- Windows SmartScreen may warn about an unknown publisher, because the exe is not code-signed. Click More info > Run anyway only if the hash matches.

## Features

Windows app (`joycon2_gui.py`):
- Connects Joy-Con 2 L and R over Bluetooth LE and shows connection state, battery, gyro and accelerometer.
- Creates a virtual DualShock 4 for Windows and Steam through ViGEmBus.
- Passes Steam's rumble to the Joy-Cons. Includes a rumble test and built-in sample patterns and songs.
- Mouse mode: use a Joy-Con as a mouse (ZR = left click, R = right click).
- Network bridge: sends the controller state to a Steam Deck over an encrypted link. While the Deck is connected, this PC does not use the Joy-Cons.

Steam Deck plugin (`decky/joycon2studio`):
- Receives controller state from the Windows app over an encrypted Wi-Fi link and presents it to Steam as a virtual DualShock 4 through `/dev/uhid`.
- Sends Steam's rumble back to the PC. Shows a pairing code, has Connect and Disconnect buttons, and a Scan network button.

## Requirements

- Windows 10 or 11 with Bluetooth LE.
- Python with tkinter (the app was built with Python 3.15).
- `pip install bleak vgamepad`
- The [ViGEmBus](https://github.com/nefarius/ViGEmBus) driver, which vgamepad needs.
- Nintendo Switch 2 Joy-Con 2 controllers. The original Joy-Con is not supported.

## Windows app

The exe in the Download section needs no Python. To run from source:

1. Install the requirements above.
2. Run `py joycon2_gui.py` from this folder.
3. Hold SYNC on each Joy-Con until the app finds it. Closing the window keeps the app running in the notification area, so you don't need SYNC when you reopen it. Right-click its icon to quit.
4. In Steam, open Settings > Controller. Steam shows the virtual controller as a DualShock 4.

To build a standalone exe:

```bat
py -m pip install pyinstaller
py -m PyInstaller JoyCon2Studio.spec
```

The exe is written to `dist\JoyCon2Studio.exe`.

## Steam Deck

### Install the plugin

1. Install Decky Loader on the Deck.
2. Copy the `decky/joycon2studio` folder to the Deck, or unzip `joycon2studio-plugin.zip` from the release.
3. In Konsole (Desktop Mode), go into that folder and run `bash install.sh`. It asks for your sudo password, installs the plugin to `~/homebrew/plugins/joycon2studio` and restarts the plugin loader. An older version is moved to `~/joycon2studio-backup-<date>`.
4. Open the Quick Access menu and pick Joy-Con 2 Studio.

### Network bridge (Joy-Cons stay on the PC)

The Deck does not pair with the Joy-Cons. The PC keeps them and sends their state to the Deck over Wi-Fi.

1. Install the plugin above and put the PC and the Deck on the same network.
2. In the Windows app, enter the Deck's IP address and the six-digit pairing code shown on the Deck, then tick "Connect to a Deck (encrypted)".
3. Steam on the Deck should show the controller, and Steam's rumble is sent back to the PC.
4. On the Deck, press **Scan network** to check that the PC is sending.

The link uses UDP port 26761. Pairing uses SPAKE2, so a recorded handshake cannot be checked offline for the six digits, and ten wrong codes from one address within a minute block that address for a minute. After pairing, every packet is encrypted and authenticated with keys made for that connection. Either side can disconnect with its Disconnect button. The Deck makes a new code after every disconnect, so enter the new code before connecting again. While the Deck is connected, this PC does not use the Joy-Cons, and when the link drops it goes back to using them.

## Troubleshooting

- Windows: the status line in the app says what failed.
- Steam Deck: in Konsole, run `journalctl -u plugin_loader -b --no-pager | tail -n 200` and send the output with the status line.

## Known limits

- Steam sees a DualShock 4, not a Joy-Con.
- The Deck does not pair with Joy-Cons over Bluetooth. Connect them to the PC.
- Gyro and accelerometer scaling are first guesses.
- The Home button is not verified in Steam.
- The Deck plugin runs as root, which `/dev/uhid` requires.
- The encryption has not been audited. Each connection uses fresh keys, so a leaked pairing code does not expose recorded past traffic. The code also changes after every disconnect; press New pairing code on the Deck to change it sooner.
- The Joy-Con 2 protocol was worked out by reverse engineering, so a firmware update may break it.

## Credits and third-party code

- Protocol details (UUIDs and the rumble setup sequence) come from joycon2cpp, a community project.
## Disclaimer

This project is not affiliated with or endorsed by Nintendo. Joy-Con and Nintendo Switch are trademarks of Nintendo. Use at your own risk.

## License

MIT. See [LICENSE](LICENSE).
