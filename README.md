# Joy-Con 2 Studio

Use Nintendo Switch 2 Joy-Con 2 controllers on Windows, on Linux (Arch and Ubuntu), on Android, and on a Steam Deck.

The Windows app connects the Joy-Cons over Bluetooth LE and presents them to Windows and Steam as a virtual DualShock 4: sticks, buttons, gyro, accelerometer and rumble. The Linux app does the same through `/dev/uhid`, and can use a Joy-Con as a mouse through `/dev/uinput`. The Steam Deck plugin (Decky Loader) can take the controllers from the PC over the network, so the Joy-Cons can stay on the PC.

> **Experimental and unofficial.** Tested on Windows 11 with a left and a right Joy-Con 2. The Linux app has not been tested on Linux hardware yet, and the Android app has not been run on a phone yet. The Steam Deck plugin uses Wi-Fi only; it does not pair over Bluetooth. The network bridge to the Deck is new and has not been tested end to end with Steam.

## Download

[Joy-Con 2 Studio for Windows](https://github.com/lexluther1307-sudo/Joy-con-2-Studio/releases/latest): download `JoyCon2Studio.exe` from the latest release. It needs no Python.

- The virtual controller needs the ViGEmBus driver. If the app shows **MISSING ViGEmBus**, click **Install ViGEmBus driver**. It downloads the official installer, checks that it is signed by Nefarius, and runs it after you approve the Windows admin prompt. If it shows **restart suggested**, a restart finishes the driver setup. It is optional.
- The app checks GitHub for a newer release when it starts. If there is one, the button at the top right changes to **Get** and the new version.

Before you run it:
- Check its SHA-256 hash against the one in the release notes: `certutil -hashfile JoyCon2Studio.exe SHA256`.
- Windows SmartScreen may warn about an unknown publisher, because the exe is not code-signed. Click More info > Run anyway only if the hash matches.

For Arch Linux and Ubuntu, build the packages from this repository. See [Linux app (Arch and Ubuntu)](#linux-app-arch-and-ubuntu).

For Android, download the debug APK from [`android/apk/`](android/apk/joycon2studio-android-0.1.0-debug.apk), or build it from this repository. See [Android app](#android-app).

## Features

Windows app (`joycon2_gui.py`):
- Connects Joy-Con 2 L and R over Bluetooth LE and shows connection state, battery, gyro and accelerometer.
- Creates a virtual DualShock 4 for Windows and Steam through ViGEmBus.
- Passes Steam's rumble to the Joy-Cons. Includes a rumble test and built-in sample patterns and songs.
- Mouse mode: use a Joy-Con as a mouse (ZR = left click, R = right click).
- Network bridge: sends the controller state to a Steam Deck over an encrypted link. While the Deck is connected, this PC does not use the Joy-Cons.

Linux app (`joycon2_gui.py` with `uhid_pad.py` and `linux_mouse.py`):
- Connects Joy-Con 2 L and R over Bluetooth LE through BlueZ, with the same screens and mouse mode as on Windows.
- Creates a virtual DualShock 4 through `/dev/uhid`, and a virtual mouse through `/dev/uinput`.
- Packaged for Arch Linux (`PKGBUILD`) and Ubuntu (`.deb`). Has the same network bridge to the Deck.

Android app (`android/`):
- Connects Joy-Con 2 L and R over Bluetooth LE and shows their connection state, sticks and pressed buttons.
- Sends the controller state to a Steam Deck over the same encrypted link, and passes the Deck's rumble to the Joy-Cons. Zero sticks and gyro works as on the PC.

Steam Deck plugin (`decky/joycon2studio`):
- Receives controller state from the PC over an encrypted Wi-Fi link and presents it to Steam as a virtual DualShock 4 through `/dev/uhid`.
- Sends Steam's rumble back to the PC. Shows a pairing code, has Connect and Disconnect buttons, and a Scan network button.
- Updates itself from the project's latest GitHub release. See [its README](decky/joycon2studio/README.md#updates).

## Setup program (Windows)

`JoyCon2Studio-Setup-<version>.exe` installs what you need in one go: the Windows app with its shortcuts and its entry in Settings > Apps, the virtual controller driver (ViGEmBus) when it is missing, and, if you choose them, the Steam Deck plugin files and the Linux files. It asks before it does anything on the Steam Deck: a console opens there, and you type the Deck's passwords in it.

- Automatic updates are off by default. The app's Settings page has a switch to check for updates when it starts, and the setup has the same switch.
- Scripted install: `JoyCon2Studio-Setup-<version>.exe --silent --dir "C:\Path\To\Folder"`. Options are listed with `--help`.
- Remove it: Settings > Apps, or `JoyCon2Studio-Setup-<version>.exe --uninstall`.

## Requirements

Windows:
- Windows 10 or 11 with Bluetooth LE.
- Python with tkinter (the app was built with Python 3.15).
- `pip install bleak vgamepad`
- The [ViGEmBus](https://github.com/nefarius/ViGEmBus) driver, which vgamepad needs.
- Nintendo Switch 2 Joy-Con 2 controllers. The original Joy-Con is not supported.

Linux (the packages install these):
- Python 3 with tkinter, `bleak` and BlueZ, and a Bluetooth adapter.
- Ubuntu 24.04 or newer, or Arch Linux.

Android:
- Android 8.0 or newer, with Bluetooth LE.
- To build the APK: JDK 17 and the Android SDK for platform 34. Android Studio installs both.

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

## Linux app (Arch and Ubuntu)

The packages are built from this repository, and the build needs only Python 3:

```bash
python3 tools/build_linux.py
```

It writes `build/linux/joycon2studio_<version>_all.deb` for Ubuntu, and `build/linux/arch/` with the PKGBUILD and its source tarball for Arch Linux.

On Ubuntu 24.04 or newer:

```bash
sudo apt install ./build/linux/joycon2studio_*_all.deb
```

On Arch Linux (`python-bleak` is in the official extra repository):

```bash
cd build/linux/arch && makepkg -si
```

Then log out and back in, or reboot, so that the access rule for `/dev/uhid` and `/dev/uinput` applies. Open **Joy-Con 2 Studio** from the application menu, or run `joycon2studio`.

- Hold SYNC on each Joy-Con, as on Windows, then press **Scan for Joy-Cons**. Pairing on Linux has not been tested on hardware.
- The virtual controller is a DualShock 4, through `/dev/uhid`. The Xbox 360 output is Windows only.
- Linux has no tray icon. Closing the window keeps the app running in the background, so the Joy-Cons stay connected. Start the app again to show the window, and use **Quit** on the About page to stop it.
- If the status line says the virtual controller needs `/dev/uhid`, or the mouse needs `/dev/uinput`, log out and back in. Or load the modules with `sudo modprobe uhid uinput`.

## Android app

The Android app does what the Windows app does for the Deck link. It connects the Joy-Cons over Bluetooth LE and sends their state to the Steam Deck over the same encrypted link. The phone is the PC side, so the Joy-Cons stay with the phone while the Deck is connected.

Build the APK on Windows, from the `android` folder:

```bat
gradlew.bat :app:assembleDebug
```

This needs JDK 17 and the Android SDK for platform 34. Android Studio sets up both and writes `android/local.properties`, which is not committed. The APK is written to `android\app\build\outputs\apk\debug\app-debug.apk`. The version 0.1.0 debug build is also in the repository at [`android/apk/joycon2studio-android-0.1.0-debug.apk`](android/apk/joycon2studio-android-0.1.0-debug.apk), with SHA-256 `c6844681ae0e1065c533b81022f1d561f9ce9740f595052a34592dbc6d1105a0`.

To install it, copy the APK to the phone and open it. When Android asks, allow installs from the app you opened it with, then tap Install. The app needs Android 8.0 or newer.

To use it:

1. Hold SYNC on each Joy-Con 2, then press **Scan for Joy-Cons**. On Android 12 and newer, allow the Nearby devices permission. On Android 8 to 11, allow Location and turn on the phone's Location switch. The app asks for it if it is off.
2. Put the phone and the Deck on the same network. On the Deck, open Joy-Con 2 Studio and note the address and the six-digit pairing code.
3. In the Android app, enter them in the Steam Deck card and press **Connect**. The screen stays on while the link is connected. **Disconnect** ends the link, and so does closing the app.

## Steam Deck

### Install the plugin

1. Install Decky Loader on the Deck.
2. Copy the `decky/joycon2studio` folder to the Deck, or unzip `joycon2studio-plugin-<version>.zip` from the release.
3. In Konsole (Desktop Mode), go into that folder and run `bash install.sh`. It asks for your sudo password, installs the plugin to `~/homebrew/plugins/joycon2studio` and restarts the plugin loader. An older version is moved to `~/joycon2studio-backup-<date>`.
4. Open the Quick Access menu and pick Joy-Con 2 Studio.

The plugin then updates itself from the latest release. See [its README](decky/joycon2studio/README.md#updates).

### Network bridge (Joy-Cons stay on the PC)

The Deck does not pair with the Joy-Cons. The PC keeps them and sends their state to the Deck over Wi-Fi.

1. Install the plugin above and put the PC and the Deck on the same network.
2. In the Windows or Linux app, enter the Deck's IP address and the six-digit pairing code shown on the Deck, then tick "Connect to a Deck (encrypted)". In the Android app, enter the same address and code and press Connect.
3. Steam on the Deck should show the controller, and Steam's rumble is sent back to the PC.
4. On the Deck, press **Scan network** to check that the PC is sending.

The link uses UDP port 26761. Pairing uses SPAKE2, so a recorded handshake cannot be checked offline for the six digits, and ten wrong codes from one address within a minute block that address for a minute. After pairing, every packet is encrypted and authenticated with keys made for that connection. Either side can disconnect with its Disconnect button. The Deck makes a new code after every disconnect, so enter the new code before connecting again. While the Deck is connected, the PC (or the phone) does not use the Joy-Cons, and when the link drops it goes back to using them.

## Troubleshooting

- Windows and Linux: the status line in the app says what failed.
- Steam Deck: in Konsole, run `journalctl -u plugin_loader -b --no-pager | tail -n 200` and send the output with the status line.

## Known limits

- Steam sees a DualShock 4, not a Joy-Con.
- The Deck does not pair with Joy-Cons over Bluetooth. Connect them to the PC.
- Gyro and accelerometer scaling are first guesses.
- The Home button is not verified in Steam.
- The Deck plugin runs as root, which `/dev/uhid` requires.
- The Linux app is not tested on Linux hardware yet. Its virtual controller and mouse were written from the Windows app and the Deck plugin, and are checked only by unit tests.
- The Linux app has no tray icon; closing its window keeps it running in the background.
- The Android app has not been run on a phone or with Joy-Cons. Its protocol and controller data are checked against the desktop code by unit tests only.
- The Android app has no mouse mode, rumble test, songs or battery readout. Its APK is a debug build, not on the Play Store.
- Automatic updates for the Deck need a published GitHub release with `joycon2studio-plugin-<version>.zip` and `SHA256SUMS.txt`. The checksum guards against a damaged download, not against a release that someone changed.
- The encryption has not been audited. Each connection uses fresh keys, so a leaked pairing code does not expose recorded past traffic. The code also changes after every disconnect; press New pairing code on the Deck to change it sooner.
- The Joy-Con 2 protocol was worked out by reverse engineering, so a firmware update may break it.

## Tests

From this folder:

```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

These cover the Deck updater, the uhid gamepad, the Linux mouse and the DualShock 4 report. They need no Joy-Cons or devices.

Android unit tests, from the `android` folder:

```bat
gradlew.bat :app:testDebugUnitTest
```

They check the Android code against the vectors that `tools/android_vectors.py` writes from the desktop code. After changing the protocol, run `py tools/android_vectors.py` from the project folder first, then the Android tests.

## Credits and third-party code

- Protocol details (UUIDs and the rumble setup sequence) come from joycon2cpp, a community project.

## Disclaimer

This project is not affiliated with or endorsed by Nintendo. Joy-Con and Nintendo Switch are trademarks of Nintendo. Use at your own risk.

## License

MIT. See [LICENSE](LICENSE).
