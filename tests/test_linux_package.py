"""Tests for the Linux packages. The .deb must list its folders before the files in them, because dpkg does not
create the folders for the files it unpacks."""

import io
import os
import posixpath
import sys
import tarfile
import tempfile
import shutil
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import build_linux as bl  # noqa: E402


def members_of(data: bytes):
    return tarfile.open(fileobj=io.BytesIO(data), mode="r:gz").getmembers()


class FolderTests(unittest.TestCase):
    def test_every_folder_above_a_file_is_listed(self):
        names = [m.name for m in members_of(bl.tar_gz([("usr/lib/app/tool.py", b"x", 0o644)], "./"))]
        for folder in ("./usr", "./usr/lib", "./usr/lib/app"):  # tarfile reads folder names without the trailing /
            self.assertIn(folder, names)
        self.assertIn("./usr/lib/app/tool.py", names)

    def test_every_folder_comes_before_the_files_inside_it(self):
        seen = set()
        for member in members_of(bl.tar_gz(bl.package_files(), "./")):
            path = member.name.rstrip("/")
            parent = posixpath.dirname(path)
            while parent not in ("", "."):
                self.assertIn(parent, seen, f"{member.name} is listed before its folder {parent}")
                parent = posixpath.dirname(parent)
            if member.isdir():
                seen.add(path)

    def test_the_arch_tarball_lists_its_folders_too(self):
        names = [m.name for m in members_of(bl.tar_gz(bl.package_files(), "joycon2studio-1.0.0/"))]
        self.assertIn("joycon2studio-1.0.0/usr", names)
        self.assertIn("joycon2studio-1.0.0/usr/lib/joycon2studio", names)

    def test_modes_and_owners_are_the_usual_ones(self):
        for member in members_of(bl.tar_gz(bl.package_files(), "./")):
            with self.subTest(name=member.name):
                if member.isdir() or member.name.endswith("usr/bin/joycon2studio"):
                    self.assertEqual(member.mode, 0o755)
                else:
                    self.assertEqual(member.mode, 0o644)
                self.assertEqual((member.uid, member.gid, member.uname), (0, 0, "root"))

    def test_the_deb_has_the_three_members_in_order(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "joycon2studio_test_all.deb")
        bl.write_deb(Path(path), "1.0.0", bl.package_files())
        with open(path, "rb") as handle:
            data = handle.read()
        self.assertEqual(data[:8], b"!<arch>\n")
        names, pos = [], 8
        while pos < len(data):
            names.append(data[pos:pos + 16].decode("ascii").strip())
            size = int(data[pos + 48:pos + 58].decode("ascii").strip())
            pos += 60 + size + (size % 2)
        self.assertEqual(names, ["debian-binary", "control.tar.gz", "data.tar.gz"])


if __name__ == "__main__":
    unittest.main()
