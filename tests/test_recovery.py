"""Synthetic account recovery, native interoperability and safe publication."""
import fcntl
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from nacl import bindings, pwhash
from PIL import Image
from photos import recovery, vault, worker, account_sign
from photos.library import Library, packed, LibraryError
from photos.sync import Client

PASSWORD = 'private recovery fixture password'


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.library = Library(self.folder/'library'); self.addCleanup(self.library.db.close)
        self.config = dict(root=str(self.library.root), node='http://127.0.0.1:18080', node_id='a'*64)
        self.client = Client(self.library, self.config)
        self.before = self.client.identity_path.read_bytes()
        self.device = self.client.device_path.read_bytes()
        self.target = self.folder/'backup.photos-recovery'

    def save(self):
        recovery.save(self.library, self.config, {}, str(self.target), PASSWORD)
        return self.target

    def empty(self):
        library = Library(self.folder/'restored'); self.addCleanup(library.db.close)
        return library, {**self.config, 'root': str(library.root)}

    def test_native_argon2id_and_xchacha_interoperate_without_leaking_plaintext(self):
        plain = b'private account fixture\0'+bytes(range(256))
        sealed = recovery.crypt(PASSWORD, plain)
        salt, nonce, cipher = sealed[:16], sealed[16:40], sealed[40:]
        key = pwhash.argon2id.kdf(32, PASSWORD.encode(), salt, opslimit=3, memlimit=64*1024*1024)
        self.assertEqual(bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(cipher, b'photos-recovery-v1', nonce, key), plain)
        fixed_salt, fixed_nonce = bytes(range(16)), bytes(range(24))
        key = pwhash.argon2id.kdf(32, PASSWORD.encode(), fixed_salt, opslimit=3, memlimit=64*1024*1024)
        cipher = bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(plain, b'photos-recovery-v1', fixed_nonce, key)
        self.assertEqual(recovery.crypt(PASSWORD, fixed_salt+fixed_nonce+cipher, restore=True), plain)
        self.assertNotEqual(sealed, recovery.crypt(PASSWORD, plain))
        self.assertNotIn(plain, sealed)

    def test_saved_file_restores_the_same_account_and_content_keys_with_a_new_device(self):
        self.save()
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertNotIn(self.client.identity['private'].encode(), self.target.read_bytes())
        self.assertNotIn(self.client.identity['content_key'].encode(), self.target.read_bytes())
        library, config = self.empty()
        recovery.restore(library, config, {}, str(self.target), PASSWORD)
        peer = Client(library, config)
        self.assertEqual(peer.identity, self.client.identity)
        self.assertEqual(peer.account, self.client.account)
        self.assertEqual(peer.key, self.client.key)
        self.assertNotEqual(peer.device_public, self.client.device_public)
        self.assertEqual(self.client.identity_path.read_bytes(), self.before)
        self.assertEqual(self.client.device_path.read_bytes(), self.device)

    def test_wrong_password_altered_file_and_mismatched_pin_never_create_keys(self):
        self.save(); library, config = self.empty()
        with self.assertRaisesRegex(LibraryError, 'Incorrect recovery password'):
            recovery.restore(library, config, {}, str(self.target), 'wrong password fixture')
        with self.assertRaisesRegex(LibraryError, 'ThinkPad pin'):
            recovery.restore(library, {**config, 'node_id': 'b'*64}, {}, str(self.target), PASSWORD)
        data = bytearray(self.target.read_bytes()); data[-1] ^= 1; self.target.write_bytes(data)
        with self.assertRaisesRegex(LibraryError, 'altered recovery file'):
            recovery.restore(library, config, {}, str(self.target), PASSWORD)
        self.assertFalse((library.root/'private/identity.json').exists())
        self.assertEqual(self.client.identity_path.read_bytes(), self.before)

    def test_existing_keys_populated_library_and_running_worker_are_never_replaced(self):
        self.save()
        with self.assertRaisesRegex(LibraryError, 'new empty library'):
            recovery.restore(self.library, self.config, {}, str(self.target), PASSWORD)
        self.assertEqual(self.client.identity_path.read_bytes(), self.before)
        library, config = self.empty()
        lock = (library.root/'.worker.lock').open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(LibraryError, 'Stop background backup'):
                recovery.restore(library, config, {}, str(self.target), PASSWORD)
        finally: lock.close()
        source = self.folder/'Camera'; source.mkdir()
        Image.new('RGB', (20, 20), 'green').save(source/'photo.jpg')
        library.import_folders([source])
        with self.assertRaisesRegex(LibraryError, 'new empty library'):
            recovery.restore(library, config, {}, str(self.target), PASSWORD)
        self.assertFalse((library.root/'private/identity.json').exists())
        self.assertEqual(library.query()['total'], 1)

    def test_hidden_lock_and_expiry_during_native_derivation_prevent_publication(self):
        token = vault.authenticate(self.library, 'Hidden fixture password', setup=True)
        with self.assertRaisesRegex(LibraryError, 'Unlock Hidden'):
            recovery.save(self.library, self.config, {}, str(self.target), PASSWORD)
        actual = recovery.crypt
        def lose_access(*args, **kwargs):
            result = actual(*args, **kwargs); vault.lock(self.library, token); return result
        with patch.object(recovery, 'crypt', side_effect=lose_access):
            with self.assertRaisesRegex(LibraryError, 'Unlock Hidden'):
                recovery.save(self.library, self.config, {'vault_token': token}, str(self.target), PASSWORD)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.client.identity_path.read_bytes(), self.before)

    def test_invalid_paths_short_password_and_existing_destination_are_preserved(self):
        for path in ['relative.file', str(self.library.root/'recovery'), str(self.folder/'missing/file')]:
            with self.assertRaises(LibraryError):
                recovery.save(self.library, self.config, {}, path, PASSWORD)
        with self.assertRaisesRegex(LibraryError, '12 characters'):
            recovery.save(self.library, self.config, {}, str(self.target), 'too short')
        self.target.write_bytes(b'owner existing file')
        with self.assertRaisesRegex(LibraryError, 'new recovery file'):
            recovery.save(self.library, self.config, {}, str(self.target), PASSWORD)
        self.assertEqual(self.target.read_bytes(), b'owner existing file')
        linked = self.folder/'linked'; linked.symlink_to(self.target)
        with self.assertRaises(LibraryError):
            recovery.save(self.library, self.config, {}, str(linked), PASSWORD)

    def test_concurrent_destination_creation_never_overwrites_the_other_file(self):
        link = os.link
        def raced(source, destination):
            Path(destination).write_bytes(b'concurrent owner file'); return link(source, destination)
        with patch.object(recovery.os, 'link', side_effect=raced):
            with self.assertRaisesRegex(LibraryError, 'already exists'):
                self.save()
        self.assertEqual(self.target.read_bytes(), b'concurrent owner file')
        self.assertFalse(list(self.folder.glob('.photos-recovery-*')))

    def test_native_malformed_frames_and_authentication_failure_emit_no_plaintext(self):
        payload = b'synthetic private fixture'
        frame = struct.pack('!BBHI', 1, 1, len(PASSWORD), len(payload))+bytes(40)+PASSWORD.encode()+payload
        frames = [b'', frame[:47], frame[:-1], frame+b'extra', b'\2'+frame[1:],
                  frame[:4]+struct.pack('!I', 65537)+frame[8:],
                  frame[:8]+b'\1'+frame[9:], frame.replace(b'private recovery', b'private\0recovery')]
        for data in frames:
            result = subprocess.run([str(recovery.program())], input=data, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 2); self.assertFalse(result.stdout); self.assertFalse(result.stderr)
        sealed = recovery.crypt(PASSWORD, payload)
        data = struct.pack('!BBHI', 1, 2, len(PASSWORD), len(sealed)-40)+sealed[:40]+PASSWORD.encode()+sealed[40:-1]+bytes([sealed[-1]^1])
        result = subprocess.run([str(recovery.program())], input=data, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1); self.assertFalse(result.stdout); self.assertFalse(result.stderr)

    def test_unavailable_native_helper_has_no_fallback_and_preserves_keys(self):
        with patch.object(recovery.subprocess, 'run', side_effect=OSError('unavailable')):
            with self.assertRaisesRegex(LibraryError, 'preserved'): self.save()
        self.assertFalse(self.target.exists())
        self.assertEqual(self.client.identity_path.read_bytes(), self.before)

    def test_mismatched_account_keypair_is_refused_before_saving(self):
        public, private = account_sign.generate_keypair()
        value = {**self.client.identity, 'private': private.hex()}
        with self.assertRaisesRegex(LibraryError, 'Invalid backup identity'):
            recovery.identity(value)

    def test_rpc_save_does_not_return_keys_password_or_private_file_contents(self):
        value = json.dumps([str(self.target), PASSWORD])
        result = worker.process(dict(action='recovery_save', view='sync', value=value), self.config)
        self.assertIn('Encrypted recovery file saved', result['status'])
        encoded = json.dumps(result)
        for secret in [PASSWORD, self.client.identity['private'], self.client.identity['content_key']]:
            self.assertNotIn(secret, encoded)


if __name__ == '__main__': unittest.main()
