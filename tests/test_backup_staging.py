"""Native phone staging must round-trip through the desktop backup format."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from photos.library import packed
from photos.sync_crypto import decrypt

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = ROOT / "build/backup-staging-test/backup_staging_probe"
CHUNK = 8 * 1024 * 1024
ACCOUNT = bytes(range(32))
KEY = bytes(reversed(range(32)))


class BackupStagingTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="staging-", dir=ROOT / "build/backup-staging-test")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.stage = self.root / "staged"
        self.stage.mkdir(mode=0o700)

    def invoke(self, size=None, key=KEY, mode=None):
        args = ["prlimit", "--core=0", "--as=100663296", "--", str(PROGRAM),
                str(self.source), str(self.stage), str(self.source.stat().st_size if size is None else size)]
        if mode:
            args.append(mode)
        return subprocess.run(args,
                              input=ACCOUNT + key, capture_output=True, timeout=60)

    def check(self, data):
        self.source.write_bytes(data)
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        value = json.loads(result.stdout)
        self.assertEqual(value["id"], hashlib.sha256(data).hexdigest())
        self.assertEqual(value["size"], len(data))
        self.assertEqual(value["manifest"]["chunk_bytes"], CHUNK)
        folder = self.stage / value["id"]
        self.assertEqual((folder / "manifest.json").read_bytes(), result.stdout)
        restored = []
        for index, digest in enumerate(value["manifest"]["chunks"]):
            blob = (folder / f"{index}.blob").read_bytes()
            self.assertEqual(hashlib.sha256(blob).hexdigest(), digest)
            aad = packed(dict(account_id=ACCOUNT.hex(), app_id="photos", id=value["id"],
                              part=f"chunk:{index}", schema="photos.media.v1")).encode()
            restored.append(decrypt(blob[24:], aad, blob[:24], KEY))
        self.assertEqual(b"".join(restored), data)
        before = {file.name: file.read_bytes() for file in folder.iterdir()}
        retry = self.invoke()
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(retry.stdout, result.stdout)
        self.assertEqual({file.name: file.read_bytes() for file in folder.iterdir()}, before)
        self.assertEqual(self.source.read_bytes(), data)
        self.assertEqual([file.name for file in self.stage.iterdir()], [value["id"]])
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        for file in folder.iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        return value

    def test_empty_binary_and_multiple_chunks_restore_and_retry_without_plaintext_files(self):
        for data in (b"", b"\0\xffprivate synthetic bytes\x80", bytes(range(256)) * (CHUNK // 256) + b"tail"):
            with self.subTest(size=len(data)):
                for folder in self.stage.iterdir():
                    for file in folder.iterdir():
                        file.unlink()
                    folder.rmdir()
                self.check(data)

    def test_wrong_declared_sizes_emit_no_manifest_or_partial_staging(self):
        self.source.write_bytes(b"fixture")
        for size in (0, 6, 8, 10**10):
            with self.subTest(size=size):
                result = self.invoke(size)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(list(self.stage.iterdir()), [])
        self.assertEqual(self.source.read_bytes(), b"fixture")

    def test_corrupted_destination_and_wrong_key_preserve_previous_chunks(self):
        value = self.check(b"original fixture")
        folder = self.stage / value["id"]
        wrong_key = self.invoke(key=b"x" * 32)
        self.assertNotEqual(wrong_key.returncode, 0)
        self.assertEqual(wrong_key.stdout, b"")
        chunk = folder / "0.blob"
        chunk.write_bytes(b"damaged encrypted chunk")
        saved = {file.name: file.read_bytes() for file in folder.iterdir()}
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual({file.name: file.read_bytes() for file in folder.iterdir()}, saved)
        self.assertEqual(list(self.stage.iterdir()), [folder])

    def test_staging_refuses_linked_root_or_existing_destination(self):
        self.source.write_bytes(b"fixture")
        destination = self.root / "other"
        destination.mkdir()
        self.stage.rmdir()
        self.stage.symlink_to(destination, target_is_directory=True)
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertEqual(list(destination.iterdir()), [])
        self.stage.unlink()
        self.stage.mkdir()
        (self.stage / hashlib.sha256(b"fixture").hexdigest()).symlink_to(destination, target_is_directory=True)
        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertEqual(list(destination.iterdir()), [])

    def test_revoked_access_or_source_change_during_staging_leaves_no_partial_files(self):
        for mode in ("cancel", "change"):
            with self.subTest(mode=mode):
                self.source.write_bytes(b"original fixture")
                result = self.invoke(mode=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(list(self.stage.iterdir()), [])
                if mode == "cancel":
                    self.assertEqual(self.source.read_bytes(), b"original fixture")
