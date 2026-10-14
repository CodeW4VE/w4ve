"""Exercise the built archive and activation, including refusal before mutation."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

import install
from tools.build_distribution import build

ROOT = Path(__file__).resolve().parents[1]


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.archive = build(ROOT, self.work / "build")
        self.version, self.files, self.manifest = install.read_archive(self.archive)
        self.prefix, self.bin = self.work / "runtime", self.work / "bin"

    def rewritten(self, mutate):
        entries = []
        with tarfile.open(self.archive) as archive:
            for item in archive:
                entries.append((item.name, archive.extractfile(item).read()))
        entries = mutate(entries)
        target = self.work / "modified.tar.gz"
        with tarfile.open(target, "w:gz") as archive:
            for name, data in entries:
                item = tarfile.TarInfo(name)
                item.size = len(data)
                archive.addfile(item, io.BytesIO(data))
        return target

    def reject(self, archive):
        with self.assertRaises((ValueError, KeyError)):
            install.install(archive, self.prefix, self.bin)
        self.assertFalse(self.bin.exists())
        self.assertFalse(self.prefix.exists())

    def test_reproducible_complete_archive(self):
        other = build(ROOT, self.work / "other")
        self.assertEqual(self.archive.read_bytes(), other.read_bytes())
        self.assertTrue(install.REQUIRED <= self.files.keys())
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).hexdigest(),
                         self.archive.with_name(self.archive.name + ".sha256")
                         .read_text().split()[0])

    def test_installed_launcher_runs_without_source_directory(self):
        launcher = install.install(self.archive, self.prefix, self.bin)
        result = subprocess.run([sys.executable, str(launcher), "--help"],
                                cwd=self.work, capture_output=True, text=True,
                                timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("install", result.stdout)
        self.assertEqual(install.verified_release(self.prefix / "releases" / self.version),
                         self.manifest)
        self.assertEqual(install.install(self.archive, self.prefix, self.bin), launcher)

    def test_modified_installed_release_preserves_launcher(self):
        launcher = install.install(self.archive, self.prefix, self.bin)
        old = launcher.read_bytes()
        (self.prefix / "releases" / self.version / "runtime.py").write_text("changed")
        with self.assertRaises(ValueError):
            install.install(self.archive, self.prefix, self.bin)
        with self.assertRaises(ValueError):
            install.activate(self.prefix / "releases" / self.version, self.bin)
        self.assertEqual(launcher.read_bytes(), old)

    def test_duplicate_entry(self):
        self.reject(self.rewritten(lambda entries: entries + [entries[0]]))

    def test_multiple_roots(self):
        self.reject(self.rewritten(lambda entries: [("other/" + entries[0][0].split("/", 1)[1],
                                                     entries[0][1])] + entries[1:]))

    def test_noncanonical_paths(self):
        for path in ("root/../outside.py", "root/./runtime.py", "root//runtime.py",
                     "/root/runtime.py", "root/C:runtime.py", "root\\runtime.py"):
            with self.subTest(path=path):
                self.reject(self.rewritten(lambda entries: [(path, b"bad")] + entries))

    def test_missing_module(self):
        self.reject(self.rewritten(lambda entries: [item for item in entries
                                                    if not item[0].endswith("/runtime.py")]))

    def test_hash_mismatch(self):
        self.reject(self.rewritten(lambda entries: [(name, b"changed" if name.endswith("/runtime.py")
                                                     else data) for name, data in entries]))

    def test_wrong_root_version(self):
        self.reject(self.rewritten(lambda entries: [("w4ve-9.9.9/" + name.split("/", 1)[1], data)
                                                    for name, data in entries]))

    def test_symlink_archive_entry(self):
        target = self.work / "symlink.tar.gz"
        with tarfile.open(target, "w:gz") as archive:
            item = tarfile.TarInfo("w4ve-" + self.version + "/runtime.py")
            item.type = tarfile.SYMTYPE
            item.linkname = "../../outside"
            archive.addfile(item)
        self.reject(target)

    def test_installed_symlink_refused(self):
        install.install(self.archive, self.prefix, self.bin)
        release = self.prefix / "releases" / self.version
        original = release / "runtime.py"
        data = original.read_bytes()
        outside = self.work / "runtime-copy.py"
        outside.write_bytes(data)
        original.unlink()
        try:
            original.symlink_to(outside)
        except OSError as exc:
            self.skipTest("host cannot create symlinks: %s" % exc)
        with self.assertRaises(ValueError):
            install.activate(release, self.bin)

    def test_manifest_paths_and_types(self):
        for value in ([], {"schema": True}, {"schema": 1, "version": 3, "files": {}},
                      {**self.manifest, "files": {**self.manifest["files"], "../outside": "0" * 64}},
                      {**self.manifest, "files": {**self.manifest["files"], "sdk": "0" * 64}}):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError):
                    install.validate_manifest(value)

    def test_rollback_checks_version_and_paths(self):
        install.install(self.archive, self.prefix, self.bin)
        release = self.prefix / "releases" / self.version
        with self.assertRaises(ValueError):
            install.verified_release(release, "9.9.9")
        manifest = {**self.manifest, "files": {**self.manifest["files"], "../outside": "0" * 64}}
        (release / "distribution.json").write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            install.activate(release, self.bin)


if __name__ == "__main__":
    unittest.main()
