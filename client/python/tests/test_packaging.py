"""Keep user-supplied executables out of both distribution archives."""
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("windows_packager", ROOT / "scripts/package-windows.py")
packager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packager)


class PackagingTests(unittest.TestCase):
    def test_runtime_archive_filter_excludes_local_programs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("DOSDrive/LOCAL.EXE", "DOSDrive/save.dat", "DOSDrive/nested/LOCAL.COM",
                         "DOSDrive/README.txt", "adapter/launch.py", ".runtime/agent.env"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("test")
                expected = name in ("DOSDrive/README.txt", "adapter/launch.py")
                self.assertEqual(packager.included(path, root), expected, name)

    def test_both_archives_exclude_even_a_tracked_local_program(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / "release/portable"
            for relative in ("BUILD-INFO.json", "DOSDrive/README.txt", "DOSDrive/LOCAL.EXE"):
                path = stage / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")
            for relative in ("DOSDrive/LOCAL.EXE", "client/python/example.py", "packaging/windows/DOSDrive-README.txt"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("test")

            def git(*args):
                if "--others" in args:
                    return b"packaging/windows/DOSDrive-README.txt\0"
                return b"DOSDrive/LOCAL.EXE\0client/python/example.py\0"

            with patch.object(packager, "ROOT", root), patch.object(packager, "git", side_effect=git), redirect_stdout(io.StringIO()):
                packager.finalize(stage)
            with zipfile.ZipFile(stage.parent / "portable.zip") as archive:
                self.assertIn("portable/DOSDrive/README.txt", archive.namelist())
                self.assertNotIn("portable/DOSDrive/LOCAL.EXE", archive.namelist())
            with zipfile.ZipFile(stage.parent / "portable-source.zip") as archive:
                self.assertIn("portable-source/client/python/example.py", archive.namelist())
                self.assertIn("portable-source/packaging/windows/DOSDrive-README.txt", archive.namelist())
                self.assertNotIn("portable-source/DOSDrive/LOCAL.EXE", archive.namelist())
