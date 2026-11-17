"""Photos' private pipe adapter must preserve Mesh v6 signed requests."""
import hashlib
import json
import struct
import subprocess
import unittest
from unittest.mock import patch

from nacl.signing import SigningKey
from pqcrypto.sign import ml_dsa_44

from photos import sync_transaction


class SyncTransactionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.public, cls.private = ml_dsa_44.generate_keypair()
        cls.seed = bytes(range(32))

    def test_existing_account_and_device_verify_exact_method_path_and_body(self):
        device = SigningKey(self.seed).verify_key
        for method, path, body in (
            ("GET", "/api/v1/records?since=42&limit=1", b""),
            ("POST", "/api/v1/sync", '家族 📷'.encode()),
            ("PUT", "/api/v1/blobs/photos/" + "a" * 64, bytes(range(256))),
            ("HEAD", "/api/v1/blobs/photos/" + "a" * 64, b""),
        ):
            with self.subTest(method=method):
                header = json.loads(sync_transaction.header(
                    self.public, self.private, self.seed, method, path, body, 2000000000))
                self.assertEqual(header["account_id"], hashlib.sha256(self.public).hexdigest())
                self.assertEqual(header["device_key_id"], hashlib.sha256(bytes(device)).hexdigest())
                self.assertEqual(header["app_id"], "photos")
                self.assertEqual(header["method"], method)
                self.assertEqual(header["path"], path)
                self.assertEqual(header["body_sha256"], hashlib.sha256(body).hexdigest())
                self.assertEqual(header["protocol_version"], 6)
                self.assertEqual(header["signature_context"], "daochi-tx-v1")
                message = "\n".join([
                    "daochi-tx-v1", "6", header["tx_id"], header["account_id"], "photos",
                    header["device_key_id"], method, path, header["body_sha256"],
                    header["nonce"], str(header["expires_at"]), "",
                ]).encode()
                self.assertTrue(ml_dsa_44.verify(self.public, message, bytes.fromhex(header["signature"])))
                self.assertEqual(device.verify(message, bytes.fromhex(header["device_signature"])), message)
                self.assertFalse(ml_dsa_44.verify(self.public, message + b"changed", bytes.fromhex(header["signature"])))

    def test_request_retries_have_fresh_nonces_and_preserve_supplied_keys(self):
        before = (self.public, self.private, self.seed)
        values = [json.loads(sync_transaction.header(*before, "POST", "/api/v1/sync", b"{}", 2000000000))
                  for _ in range(4)]
        self.assertEqual(len({value["nonce"] for value in values}), 4)
        self.assertEqual(len({value["tx_id"] for value in values}), 4)
        self.assertEqual(before, (self.public, self.private, self.seed))

    def test_partial_frames_trailing_data_and_unsafe_paths_emit_nothing(self):
        def frame(path=b"/api/v1/sync"):
            return (struct.pack("!BBHQ", 1, 2, len(path), 2000000000)
                    + self.public + self.private + self.seed + hashlib.sha256(b"{}").digest() + path)

        valid = frame()
        for value in (b"", valid[:11], valid[:-1], valid + b"extra", b"\0" + valid[1:],
                      valid[:1] + b"\6" + valid[2:], frame(b"relative"),
                      frame(b"/api/v1/sync\nother"), frame(b"/api/v1/sync\0other")):
            with self.subTest(length=len(value)):
                result = subprocess.run([str(sync_transaction.program())], input=value,
                                        capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(result.stderr, b"")

    def test_missing_or_interrupted_signer_cannot_fall_back(self):
        for error in (OSError(), subprocess.TimeoutExpired("Mesh signing", 30)):
            with patch.object(sync_transaction.subprocess, "run", side_effect=error):
                with self.assertRaisesRegex(ValueError, "preserved"):
                    sync_transaction.header(self.public, self.private, self.seed,
                                            "POST", "/api/v1/sync", b"{}", 2000000000)
