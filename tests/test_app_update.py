"""Tests for the Windows app's in-app update: finding the files, checking the download, and the helper that swaps
the exe. No network is used: the release and the download are replaced by fakes."""

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import joycon2_gui as g  # noqa: E402

PREFIX = g.UPDATE_DOWNLOAD_PREFIX


def release(version="v0.2.14", exe=True, sums=True, base=PREFIX):
    assets = []
    if exe:
        assets.append({"name": "JoyCon2Studio.exe", "browser_download_url": f"{base}{version}/JoyCon2Studio.exe"})
    if sums:
        assets.append({"name": "SHA256SUMS.txt", "browser_download_url": f"{base}{version}/SHA256SUMS.txt"})
    return {"tag_name": version, "html_url": "https://example.com/page", "assets": assets}


class FakeReply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class AssetTests(unittest.TestCase):
    def test_both_files_are_found(self):
        exe, sums = g.update_assets(release())
        self.assertTrue(exe.endswith("/JoyCon2Studio.exe"))
        self.assertTrue(sums.endswith("/SHA256SUMS.txt"))

    def test_a_release_without_one_of_the_files_cannot_be_installed(self):
        self.assertIsNone(g.update_assets(release(exe=False)))
        self.assertIsNone(g.update_assets(release(sums=False)))

    def test_files_from_elsewhere_are_refused(self):
        self.assertIsNone(g.update_assets(release(base="https://example.com/")))

    def test_a_newer_release_is_reported_with_its_files(self):
        body = json.dumps(release("v0.2.14")).encode("utf-8")
        with mock.patch.object(g, "APP_VERSION", "0.2.13"), \
                mock.patch.object(g.urllib.request, "urlopen", lambda req, timeout=None: FakeReply(body)):
            newer, latest, url, assets = g.check_for_update()
        self.assertTrue(newer)
        self.assertEqual(latest, "v0.2.14")
        self.assertIsNotNone(assets)

    def test_the_same_version_is_not_newer(self):
        body = json.dumps(release("v0.2.14")).encode("utf-8")
        with mock.patch.object(g, "APP_VERSION", "0.2.14"), \
                mock.patch.object(g.urllib.request, "urlopen", lambda req, timeout=None: FakeReply(body)):
            newer, _, _, _ = g.check_for_update()
        self.assertFalse(newer)


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.path = os.path.join(self.folder, "JoyCon2Studio.new.exe")
        with open(self.path, "wb") as handle:
            handle.write(b"new exe bytes")
        self.digest = hashlib.sha256(b"new exe bytes").hexdigest()

    def test_a_matching_checksum_passes(self):
        self.assertTrue(g.verify_download(self.path, f"{self.digest}  JoyCon2Studio.exe\n"))

    def test_the_binary_marker_in_the_checksum_file_is_accepted(self):
        self.assertTrue(g.verify_download(self.path, f"{self.digest} *JoyCon2Studio.exe\n"))

    def test_a_wrong_checksum_fails(self):
        self.assertFalse(g.verify_download(self.path, f"{'0' * 64}  JoyCon2Studio.exe\n"))

    def test_a_checksum_file_without_the_exe_fails(self):
        self.assertFalse(g.verify_download(self.path, f"{self.digest}  JoyCon2Studio-Setup-0.2.14.exe\n"))


class HelperTests(unittest.TestCase):
    def script(self):
        return g.update_helper_script(r"C:\Apps X\JoyCon2Studio.exe", r"C:\Apps X\JoyCon2Studio.new.exe",
                                      r"C:\Apps X\JoyCon2Studio.previous.exe", 4242, r"C:\Temp\update.log")

    def test_it_waits_for_the_app_to_close_before_it_swaps_the_exe(self):
        text = self.script()
        self.assertIn("set PID=4242", text)
        self.assertLess(text.index('PID eq %PID%'), text.index('move /Y "%NEW%" "%CURRENT%"'))

    def test_it_starts_the_new_exe_and_deletes_itself(self):
        text = self.script()
        self.assertLess(text.index('move /Y "%NEW%" "%CURRENT%"'), text.index('start "" "%CURRENT%"'))
        self.assertIn('(goto) 2>nul & del "%~f0"', text)

    def test_paths_with_spaces_are_quoted(self):
        self.assertIn('set "CURRENT=C:\\Apps X\\JoyCon2Studio.exe"', self.script())

    def test_the_script_uses_windows_line_endings(self):
        text = self.script()
        self.assertTrue(text.endswith("\r\n"))
        self.assertEqual(text.count("\n"), text.count("\r\n"))


class DownloadTests(unittest.TestCase):
    def test_the_download_is_written_and_progress_is_reported(self):
        body = b"x" * 200000
        seen = []
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        dest = os.path.join(folder, "new.exe")

        class Reply(FakeReply):
            headers = {"Content-Length": str(len(body))}

        with mock.patch.object(g.urllib.request, "urlopen", lambda req, timeout=None: Reply(body)):
            g.download_update("https://example.com/x", dest, progress=seen.append)
        self.assertEqual(os.path.getsize(dest), len(body))
        self.assertAlmostEqual(seen[-1], 1.0)


class GateTests(unittest.TestCase):
    def test_the_in_app_install_is_only_offered_for_the_built_app(self):
        self.assertFalse(g.can_install_in_app())  # the tests run from source, not as the built exe


if __name__ == "__main__":
    unittest.main()
