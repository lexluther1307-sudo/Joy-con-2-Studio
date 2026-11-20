"""Tests for the Deck plugin's updater. No network is used: GitHub and the downloads are replaced by fakes."""

import asyncio
import hashlib
import io
import json
import os
import shutil
import ssl
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.join(os.path.dirname(HERE), "decky", "joycon2studio")
sys.path.insert(0, PLUGIN_DIR)

import updater  # noqa: E402
from updater import UpdateError, UpdateManager  # noqa: E402


def make_zip(path, files, links=()):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo("placeholder")
            info.filename = name  # keep the name exactly as given (zipfile rewrites backslashes on Windows)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
        for name, target in links:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = 0o120777 << 16  # a symbolic link
            archive.writestr(info, target)


def plugin_files(version="0.2.9", extra=None):
    files = {
        "joycon2studio/plugin.json": "{}",
        "joycon2studio/main.py": f'VERSION = "{version}"\n',
        "joycon2studio/joycon2_link.py": "X = 1\n",
        "joycon2studio/dist/index.js": "// bundle\n",
    }
    files.update(extra or {})
    return files


def write_tree(root, files):
    for rel, text in files.items():
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class FakeReply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeLog:
    def __init__(self):
        self.lines = []

    def info(self, text):
        self.lines.append(("info", text))

    def error(self, text):
        self.lines.append(("error", text))


class VersionTests(unittest.TestCase):
    def test_parses_plain_and_v_prefixed_versions(self):
        self.assertEqual(updater.parse_version("v0.2.9"), (0, 2, 9))
        self.assertEqual(updater.parse_version("0.10.0"), (0, 10, 0))

    def test_rejects_other_text(self):
        for bad in ("0.2", "x.y.z", "", None, "1.2.3.4"):
            with self.assertRaises(UpdateError):
                updater.parse_version(bad)

    def test_compares_numerically(self):
        self.assertTrue(updater.is_newer("0.2.9", "0.2.8"))
        self.assertTrue(updater.is_newer("0.10.0", "0.9.9"))
        self.assertFalse(updater.is_newer("0.2.8", "0.2.8"))
        self.assertFalse(updater.is_newer("0.2.7", "0.2.8"))


class ChecksumTests(unittest.TestCase):
    def test_finds_the_named_file(self):
        text = f"{'a' * 64}  joycon2studio-plugin-0.2.9.zip\n{'b' * 64} *other.exe\n"
        self.assertEqual(updater.expected_sha256(text, "joycon2studio-plugin-0.2.9.zip"), "a" * 64)
        self.assertEqual(updater.expected_sha256(text, "other.exe"), "b" * 64)
        self.assertIsNone(updater.expected_sha256(text, "missing.zip"))


class ReleaseTests(unittest.TestCase):
    def fake_api(self, data):
        body = json.dumps(data).encode("utf-8")
        return mock.patch.object(updater.urllib.request, "urlopen",
                                 lambda req, timeout=None, context=None: FakeReply(body))

    def release(self, version="0.2.9", zip_url=None, sums_url=None, tag=None):
        base = updater.DOWNLOAD_PREFIX + f"v{version}/"
        return {"tag_name": tag or f"v{version}", "assets": [
            {"name": f"joycon2studio-plugin-{version}.zip",
             "browser_download_url": zip_url or base + f"joycon2studio-plugin-{version}.zip"},
            {"name": "SHA256SUMS.txt", "browser_download_url": sums_url or base + "SHA256SUMS.txt"},
        ]}

    def test_finds_the_plugin_zip_and_checksums(self):
        with self.fake_api(self.release()):
            found = updater.latest_release()
        self.assertEqual(found["version"], "0.2.9")
        self.assertEqual(found["zip_name"], "joycon2studio-plugin-0.2.9.zip")

    def test_release_without_the_zip_is_refused(self):
        data = self.release()
        data["assets"] = data["assets"][1:]
        with self.fake_api(data), self.assertRaises(UpdateError):
            updater.latest_release()

    def test_files_from_elsewhere_are_refused(self):
        with self.fake_api(self.release(zip_url="https://example.com/joycon2studio-plugin-0.2.9.zip")), \
                self.assertRaises(UpdateError):
            updater.latest_release()

    def test_download_refuses_other_addresses(self):
        with self.assertRaises(UpdateError):
            updater.download("https://example.com/x.zip", os.devnull, 10)


class UnpackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.out)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def zip_path(self, files, links=()):
        path = os.path.join(self.tmp, "plugin.zip")
        make_zip(path, files, links)
        return path

    def test_unpacks_the_plugin_files_without_the_folder_prefix(self):
        updater.unpack(self.zip_path(plugin_files()), self.out)
        self.assertTrue(os.path.isfile(os.path.join(self.out, "main.py")))
        self.assertTrue(os.path.isfile(os.path.join(self.out, "dist", "index.js")))

    def test_refuses_names_that_leave_the_folder(self):
        for name in ("joycon2studio/../evil.py", "/abs/evil.py", "other/evil.py"):
            with self.subTest(name=name):
                files = plugin_files(extra={name: "print(1)\n"})
                with self.assertRaises(UpdateError):
                    updater.unpack(self.zip_path(files), self.out)

    def test_refuses_backslash_names_from_other_tools(self):
        info = mock.Mock(filename="joycon2studio/a\\b.py", external_attr=0)
        info.is_dir.return_value = False
        archive = mock.MagicMock()
        archive.__enter__.return_value = archive
        archive.infolist.return_value = [info]
        with mock.patch.object(updater.zipfile, "ZipFile", return_value=archive), \
                self.assertRaises(UpdateError):
            updater.unpack("ignored.zip", self.out)

    def test_refuses_links(self):
        path = self.zip_path(plugin_files(), links=[("joycon2studio/link.py", "/etc/passwd")])
        with self.assertRaises(UpdateError):
            updater.unpack(path, self.out)


class PackageCheckTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        write_tree(self.root, {k.split("/", 1)[1]: v for k, v in plugin_files().items()})

    def test_a_complete_package_passes(self):
        updater.check_package(self.root, "0.2.9")

    def test_a_missing_file_fails(self):
        os.remove(os.path.join(self.root, "dist", "index.js"))
        with self.assertRaises(UpdateError):
            updater.check_package(self.root, "0.2.9")

    def test_a_syntax_error_fails(self):
        write_tree(self.root, {"joycon2_link.py": "def (\n"})
        with self.assertRaises(UpdateError):
            updater.check_package(self.root, "0.2.9")

    def test_a_different_version_fails(self):
        with self.assertRaises(UpdateError):
            updater.check_package(self.root, "0.3.0")


class SwapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.plugin = os.path.join(self.tmp, "plugin")
        self.backup = os.path.join(self.tmp, "backup")
        write_tree(self.plugin, {"main.py": "old"})

    def test_new_plugin_takes_the_place_and_the_old_one_is_kept(self):
        staged = os.path.join(self.tmp, "staged")
        write_tree(staged, {"main.py": "new"})
        updater.swap_in(staged, self.plugin, self.backup)
        self.assertEqual(read(os.path.join(self.plugin, "main.py")), "new")
        self.assertEqual(read(os.path.join(self.backup, "main.py")), "old")
        self.assertFalse(os.path.exists(staged))

    def test_a_failed_swap_puts_the_old_plugin_back(self):
        with self.assertRaises(OSError):
            updater.swap_in(os.path.join(self.tmp, "missing"), self.plugin, self.backup)
        self.assertEqual(read(os.path.join(self.plugin, "main.py")), "old")


class FakeClock:
    """Stands in for the network: serves one release and one zip."""

    def __init__(self, zip_bytes, version="0.2.9", sums_sha=None):
        self.zip_bytes = zip_bytes
        self.version = version
        self.sums_sha = sums_sha if sums_sha is not None else hashlib.sha256(zip_bytes).hexdigest()
        self.downloads = 0

    def release(self):
        name = f"joycon2studio-plugin-{self.version}.zip"
        return {"version": self.version, "zip_name": name, "zip_url": updater.DOWNLOAD_PREFIX + name,
                "sums_url": updater.DOWNLOAD_PREFIX + "SHA256SUMS.txt"}

    def download(self, url, dest, limit):
        self.downloads += 1
        with open(dest, "wb") as handle:
            handle.write(self.zip_bytes)

    def sums(self, url):
        return f"{self.sums_sha}  joycon2studio-plugin-{self.version}.zip\n"


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.plugin = os.path.join(self.tmp, "plugin")
        write_tree(self.plugin, {"main.py": 'VERSION = "0.2.8"\n'})
        self.settings = os.path.join(self.tmp, "settings")
        self.restarts = []
        self.log = FakeLog()
        self.pc_connected = False

    def manager(self):
        m = UpdateManager(current="0.2.8", plugin_dir=self.plugin, settings_dir=self.settings,
                          can_update=lambda: not self.pc_connected, log=self.log,
                          restart=lambda: self.restarts.append(1))
        m._lock = asyncio.Lock()  # normally made by start(), which needs a running Deck loop
        return m

    def run_check(self, m, clock):
        with mock.patch.object(updater, "latest_release", clock.release), \
                mock.patch.object(updater, "download", clock.download), \
                mock.patch.object(updater, "fetch_sums", clock.sums):
            return asyncio.run(m.check())

    def zip_bytes(self, version="0.2.9"):
        path = os.path.join(self.tmp, f"src-{version}.zip")
        make_zip(path, plugin_files(version))
        with open(path, "rb") as handle:
            return handle.read()

    def test_a_newer_release_is_installed_and_the_plugin_restarts(self):
        m = self.manager()
        result = self.run_check(m, FakeClock(self.zip_bytes()))
        self.assertEqual(result, "done")
        self.assertEqual(m.state, "restarting")
        self.assertEqual(self.restarts, [1])
        self.assertEqual(read(os.path.join(self.plugin, "main.py")), 'VERSION = "0.2.9"\n')
        self.assertEqual(read(os.path.join(self.settings, "update", "backup", "main.py")), 'VERSION = "0.2.8"\n')

    def test_a_newer_release_waits_while_a_pc_is_connected(self):
        self.pc_connected = True
        clock = FakeClock(self.zip_bytes())
        result = self.run_check(self.manager(), clock)
        self.assertEqual(result, "deferred")
        self.assertEqual(clock.downloads, 0)
        self.assertEqual(self.restarts, [])
        self.assertEqual(read(os.path.join(self.plugin, "main.py")), 'VERSION = "0.2.8"\n')

    def test_the_same_version_does_not_download(self):
        clock = FakeClock(self.zip_bytes("0.2.8"), version="0.2.8")
        m = self.manager()
        self.assertEqual(self.run_check(m, clock), "done")
        self.assertEqual(m.state, "uptodate")
        self.assertEqual(clock.downloads, 0)

    def test_a_checksum_mismatch_keeps_the_installed_plugin(self):
        m = self.manager()
        result = self.run_check(m, FakeClock(self.zip_bytes(), sums_sha="0" * 64))
        self.assertEqual(result, "error")
        self.assertIn("does not match", m.message)
        self.assertEqual(read(os.path.join(self.plugin, "main.py")), 'VERSION = "0.2.8"\n')
        self.assertEqual(self.restarts, [])

    def test_a_failed_check_is_reported(self):
        m = self.manager()

        def broken():
            raise OSError("no network")

        with mock.patch.object(updater, "latest_release", broken):
            self.assertEqual(asyncio.run(m.check()), "error")
        self.assertIn("no network", m.message)

    def test_a_check_already_running_is_not_started_again(self):
        m = self.manager()
        asyncio.run(self._hold_and_check(m))
        self.assertEqual(self.restarts, [])

    async def _hold_and_check(self, m):
        await m._lock.acquire()
        try:
            self.assertEqual(await m.check(), "busy")
        finally:
            m._lock.release()

    def test_the_automatic_setting_is_kept_between_runs(self):
        m = self.manager()
        self.assertTrue(m.status()["enabled"])
        m.set_auto(False)
        self.assertFalse(self.manager().status()["enabled"])


class TlsTests(unittest.TestCase):
    def test_uses_the_system_bundle_when_it_exists(self):
        bundle = updater.SYSTEM_CA_BUNDLES[1]
        with mock.patch.object(updater.os.path, "isfile", side_effect=lambda path: path == bundle), \
                mock.patch.object(updater.ssl, "create_default_context") as make:
            updater.tls_context()
        make.assert_called_once_with(cafile=bundle)

    def test_falls_back_to_the_default_when_no_bundle_exists(self):
        with mock.patch.object(updater.os.path, "isfile", return_value=False), \
                mock.patch.object(updater.ssl, "create_default_context") as make:
            updater.tls_context()
        make.assert_called_once_with()

    def test_a_release_is_read_with_a_verifying_context(self):
        seen = []

        def opener(req, timeout=None, context=None):
            seen.append(context)
            return FakeReply(json.dumps({"tag_name": "v0.2.9", "assets": []}).encode("utf-8"))

        with mock.patch.object(updater.urllib.request, "urlopen", opener), self.assertRaises(UpdateError):
            updater.latest_release()  # the answer has no release files, so the check stops there
        self.assertIsInstance(seen[0], ssl.SSLContext)
        self.assertEqual(seen[0].verify_mode, ssl.CERT_REQUIRED)


if __name__ == "__main__":
    unittest.main()
