"""Native mobile account recovery must accept exactly the desktop file format."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import unittest

from photos import account_sign, recovery
from photos.library import packed

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = ROOT / "build/backup-identity-test/backup_identity_probe"
PASSWORD = "Synthetic recovery password 🔒"


class BackupIdentityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        public, private = account_sign.generate_keypair()
        cls.value = dict(version=1, node_id="ab" * 32, account=hashlib.sha256(public).hexdigest(),
                         identity=dict(public=public.hex(), private=private.hex(), content_key="cd" * 32))
        cls.file = cls.seal(cls.value)

    @staticmethod
    def seal(value, text=None):
        return recovery.MAGIC + recovery.crypt(PASSWORD, (packed(value) if text is None else text).encode())

    def invoke(self, file=None, password=PASSWORD):
        secret = password.encode()
        file = self.file if file is None else file
        result = subprocess.run(["prlimit", "--core=0", "--as=536870912", "--cpu=60", "--", str(PROGRAM)],
                                input=struct.pack("!HI", len(secret), len(file)) + secret + file,
                                capture_output=True, timeout=65)
        self.assertEqual(result.stderr, b"")
        return result

    def refused(self, file=None, password=PASSWORD):
        result = self.invoke(file, password)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")

    def test_desktop_file_restores_same_account_and_thinkpad_pin(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, bytes.fromhex(self.value["node_id"] + self.value["account"]))

    def test_incorrect_password_altered_file_and_truncated_file_emit_no_identity(self):
        self.refused(password="Another synthetic password")
        self.refused(self.file[:-1] + bytes([self.file[-1] ^ 1]))
        self.refused(self.file[:-1])
        self.refused(b"x" + self.file[1:])

    def test_recovery_rejects_unmatched_account_or_signing_keys(self):
        for field in ("account", "public", "private"):
            with self.subTest(field=field):
                value = copy.deepcopy(self.value)
                target = value if field == "account" else value["identity"]
                target[field] = ("00" if target[field][:2] != "00" else "ff") + target[field][2:]
                self.refused(self.seal(value))

    def test_recovery_requires_exact_fields_integer_version_and_valid_key_lengths(self):
        for change in ("version", "extra", "extra_key", "short_key", "invalid_pin", "duplicate"):
            with self.subTest(change=change):
                value = copy.deepcopy(self.value)
                text = None
                if change == "version": value["version"] = 1.0
                if change == "extra": value["unexpected"] = True
                if change == "extra_key": value["identity"]["unexpected"] = True
                if change == "short_key": value["identity"]["content_key"] = "cd" * 31
                if change == "invalid_pin": value["node_id"] = "gh" * 32
                if change == "duplicate": text = packed(value)[:-1] + ',"version":1}'
                self.refused(self.seal(value, text))

    def test_password_character_minimum_is_not_a_utf8_byte_minimum(self):
        self.refused(password="🔒" * 11)
        secret = "🔒" * 12
        file = recovery.MAGIC + recovery.crypt(secret, packed(self.value).encode())
        self.assertEqual(self.invoke(file, secret).returncode, 0)
