"""Tests for the Windows setup program's logic. Nothing is installed: folders are temporary, and the Windows steps
(registry, shortcuts, the driver) are replaced by fakes."""

import base64
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "installer"))

import joycon2studio_setup as s  # noqa: E402


def options_for(target, **changes):
    options = {
        "target": target, "start_menu": False, "desktop": False, "register": False, "driver": False,
        "deck_host": "", "deck_auto_updates": False, "open_deck": False, "linux_dir": "", "check_updates": False,
    }
    options.update(changes)
    return options


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_the_update_check_is_set_and_other_keys_are_kept(self):
        with open(os.path.join(self.folder, "settings.json"), "w", encoding="utf-8") as handle:
            handle.write('{"bridge_ip": "10.0.0.5", "check_updates": true}')
        s.write_app_settings(self.folder, check_updates=False)
        with open(os.path.join(self.folder, "settings.json"), encoding="utf-8") as handle:
            data = handle.read()
        self.assertIn('"bridge_ip": "10.0.0.5"', data)
        self.assertIn('"check_updates": false', data)

    def test_a_damaged_settings_file_is_replaced(self):
        with open(os.path.join(self.folder, "settings.json"), "w", encoding="utf-8") as handle:
            handle.write("[1, 2")
        s.write_app_settings(self.folder, check_updates=False)
        with open(os.path.join(self.folder, "settings.json"), encoding="utf-8") as handle:
            self.assertIn('"check_updates": false', handle.read())


class DeckScriptTests(unittest.TestCase):
    def test_the_script_copies_the_plugin_and_runs_install_sh(self):
        text = s.deck_script_text("example-deck.local", r"C:\plugin\joycon2studio", auto_updates=True)
        self.assertIn("tar -cf -", text)
        self.assertIn("bash install.sh", text)
        self.assertIn("ssh -t deck@example-deck.local", text)
        self.assertNotIn("update.json", text)  # automatic updates on: nothing is written

    def test_automatic_updates_off_writes_the_setting_before_install_sh(self):
        text = s.deck_script_text("example-deck.local", r"C:\plugin", auto_updates=False)
        payload = base64.b64encode(b'{"auto_update": false, "last_check": 0}\n').decode("ascii")
        self.assertIn(payload, text)
        self.assertLess(text.index("update.json"), text.index("bash install.sh"))

    def test_addresses_that_could_inject_commands_are_refused(self):
        for bad in ("", "deck;rm -rf ~", "a b", "-x", "deck&calc"):
            with self.subTest(host=bad), self.assertRaises(ValueError):
                s.deck_script_text(bad, r"C:\plugin", auto_updates=True)

    def test_normal_addresses_are_accepted(self):
        for good in ("192.168.1.10", "steamdeck.local", "deck-1.lan"):
            with self.subTest(host=good):
                self.assertTrue(s.valid_host(good))

    def test_the_script_uses_windows_line_endings(self):
        text = s.deck_script_text("example-deck.local", r"C:\plugin", auto_updates=True)
        self.assertTrue(text.endswith("\r\n"))
        self.assertEqual(text.count("\n"), text.count("\r\n"))  # every line break is CR LF, none is bare LF


class UninstallTests(unittest.TestCase):
    def test_the_uninstall_entry_names_the_folder_and_the_version(self):
        values = s.uninstall_values(r"C:\Apps\JCS", "0.2.12", r"C:\Apps\JCS\Uninstall.exe")
        self.assertEqual(values["UninstallString"], '"C:\\Apps\\JCS\\Uninstall.exe" --uninstall --dir "C:\\Apps\\JCS"')
        self.assertEqual(values["DisplayVersion"], "0.2.12")
        self.assertEqual(values["InstallLocation"], r"C:\Apps\JCS")


class InstallStepTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_steps_run_in_order_and_unchosen_ones_are_skipped(self):
        events = []
        options = options_for(os.path.join(self.folder, "app"))
        with mock.patch.object(s, "copy_app", lambda target: events.append("copy")), \
                mock.patch.object(s, "write_app_settings", lambda *args: events.append("settings")):
            failures = s.install_all(options, lambda text: None, None, lambda key, status: events.append((key, status)))
        self.assertEqual(failures, [])
        self.assertEqual(events[0], ("app", "running"))
        self.assertIn(("shortcuts", "skipped"), events)
        self.assertIn(("driver", "skipped"), events)
        self.assertIn(("linux", "skipped"), events)

    def test_a_failed_step_is_reported_and_the_others_still_run(self):
        linux_dir = os.path.join(self.folder, "linux")
        options = options_for(os.path.join(self.folder, "app"), linux_dir=linux_dir)
        with mock.patch.object(s, "copy_app", side_effect=RuntimeError("boom")), \
                mock.patch.object(s, "write_app_settings", lambda *args: None):
            failures = s.install_all(options, lambda text: None, None, lambda key, status: None)
        self.assertTrue(any("boom" in text for text in failures))
        self.assertTrue(os.path.isfile(os.path.join(linux_dir, "README - Linux.txt")))

    def test_the_steam_deck_step_writes_the_install_script_into_the_target(self):
        target = os.path.join(self.folder, "app")
        plugin = os.path.join(target, "Steam Deck plugin", "joycon2studio")
        os.makedirs(plugin)
        options = options_for(target, deck_host="example-deck.local")
        with mock.patch.object(s, "copy_app", lambda folder: None), \
                mock.patch.object(s, "write_app_settings", lambda *args: None), \
                mock.patch.object(s, "unpack_deck_plugin", lambda folder: plugin):
            failures = s.install_all(options, lambda text: None, None, lambda key, status: None)
        self.assertEqual(failures, [])
        self.assertTrue(os.path.isfile(os.path.join(target, "Install on the Steam Deck.cmd")))

    def test_a_setup_without_the_deck_option_does_not_touch_the_deck(self):
        target = os.path.join(self.folder, "app")
        with mock.patch.object(s, "copy_app", lambda folder: None), \
                mock.patch.object(s, "write_app_settings", lambda *args: None), \
                mock.patch.object(s, "unpack_deck_plugin", side_effect=AssertionError("must not run")):
            failures = s.install_all(options_for(target), lambda text: None, None, lambda key, status: None)
        self.assertEqual(failures, [])


class LinuxFilesTests(unittest.TestCase):
    def test_the_linux_files_are_copied_with_a_readme(self):
        payload = os.path.join(tempfile.mkdtemp(), "payload")
        self.addCleanup(shutil.rmtree, os.path.dirname(payload), True)
        os.makedirs(os.path.join(payload, "linux"))
        with open(os.path.join(payload, "linux", "install-linux.sh"), "w", encoding="utf-8") as handle:
            handle.write("#!/usr/bin/env bash\n")
        dest = os.path.join(os.path.dirname(payload), "out")
        with mock.patch.object(s, "payload_dir", lambda: payload):
            s.copy_linux(dest)
        self.assertTrue(os.path.isfile(os.path.join(dest, "install-linux.sh")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "README - Linux.txt")))


class ArgumentTests(unittest.TestCase):
    def test_updates_are_off_unless_asked_for(self):
        args = s.parse_args(["--silent"])
        self.assertEqual(args.check_updates, "off")
        self.assertEqual(args.deck_auto_updates, "off")
        self.assertFalse(args.uninstall)


class LinuxScriptTests(unittest.TestCase):
    SCRIPT = os.path.join(ROOT, "installer", "linux", "install-linux.sh")

    def test_the_script_has_unix_line_endings(self):
        with open(self.SCRIPT, "rb") as handle:
            self.assertNotIn(b"\r\n", handle.read())

    def test_the_script_parses_as_bash(self):
        bash = r"C:\Program Files\Git\bin\bash.exe"
        if not os.path.exists(bash):
            self.skipTest("Git Bash is not installed")
        result = subprocess.run([bash, "-n", self.SCRIPT], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
