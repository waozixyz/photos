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
from photos import recovery, vault, worker, account_sign, app_lock, guest, access_sessions
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

    def reset_request(self, kind='app', password=PASSWORD, replacement='new local fixture password'):
        return dict(action='recovery_reset_'+kind, value=json.dumps([str(self.target), password, replacement]))

    def lock_both(self):
        hidden = vault.authenticate(self.library, 'old Hidden fixture password', setup=True)
        app_lock.route(self.library, dict(action='app_lock_password', value='old local fixture password'))
        app = app_lock.route(self.library, dict(action='app_lock_unlock', value='old local fixture password'))['app_lock_token']
        return hidden, app

    def test_recovery_resets_app_password_from_locked_screen_without_unlocking_hidden(self):
        self.save(); hidden, app = self.lock_both()
        before_hidden = (self.library.root/'private/hidden-password.json').read_bytes()
        result = worker.process(self.reset_request(), self.config)
        self.assertEqual(result['recovery_reset'], 1); self.assertEqual(result['app_locked'], 1)
        self.assertFalse(result['photos']); self.assertFalse(result['albums'])
        self.assertNotIn('app_lock_token', result)
        self.assertFalse(app_lock.unlocked(self.library, app)); self.assertFalse(vault.unlocked(self.library, hidden))
        self.assertEqual((self.library.root/'private/hidden-password.json').read_bytes(), before_hidden)
        self.assertEqual(worker.process(dict(action='app_lock_unlock', value='old local fixture password'), self.config)['app_locked'], 1)
        accepted = worker.process(dict(action='app_lock_unlock', value='new local fixture password'), self.config)
        self.assertEqual(accepted['app_lock_authorized'], 1); self.assertFalse(accepted['vault_unlocked'])
        self.assertEqual(self.client.identity_path.read_bytes(), self.before)
        self.assertEqual(self.client.device_path.read_bytes(), self.device)
        for secret in [PASSWORD, 'new local fixture password', self.client.identity['content_key']]:
            self.assertNotIn(secret, json.dumps(result))

    def test_hidden_reset_keeps_independent_app_password_and_backup_keys(self):
        self.save(); hidden, app = self.lock_both(); app_before = app_lock.path(self.library).read_bytes()
        result = worker.process(self.reset_request('hidden'), self.config)
        self.assertEqual(result['recovery_reset'], 1); self.assertEqual(result['app_locked'], 1)
        self.assertEqual(app_lock.path(self.library).read_bytes(), app_before)
        self.assertFalse(app_lock.unlocked(self.library, app)); self.assertFalse(vault.unlocked(self.library, hidden))
        with self.assertRaisesRegex(LibraryError, 'Incorrect password'):
            vault.authenticate(self.library, 'old Hidden fixture password')
        self.assertTrue(vault.unlocked(self.library, vault.authenticate(self.library, 'new local fixture password')))
        app_lock.authenticate(self.library, 'old local fixture password')
        self.assertEqual(self.client.identity_path.read_bytes(), self.before)

    def test_recovering_hidden_updates_shared_app_verifier_without_granting_access(self):
        self.save(); vault.authenticate(self.library, 'old Hidden fixture password', setup=True)
        app_lock.route(self.library, dict(action='app_lock_enable', value='old Hidden fixture password'))
        result = worker.process(self.reset_request('hidden'), self.config)
        self.assertEqual(result['recovery_reset'], 1); self.assertEqual(result['app_locked'], 1)
        self.assertEqual(app_lock.settings(self.library)['method'], 'hidden')
        app_lock.authenticate(self.library, 'new local fixture password')
        self.assertFalse(vault.unlocked(self.library, result.get('vault_token', '')))

    def test_wrong_proof_pin_account_and_changed_content_key_preserve_every_credential(self):
        self.save(); self.lock_both()
        paths = [app_lock.path(self.library), self.library.root/'private/hidden-password.json', self.client.identity_path, self.client.device_path]
        before = [p.read_bytes() for p in paths]
        for request, config in [(self.reset_request(password='wrong recovery fixture password'), self.config),
                                (self.reset_request(), {**self.config, 'node_id': 'b'*64})]:
            result = worker.process(request, config)
            self.assertEqual(result['recovery_reset'], 0); self.assertEqual([p.read_bytes() for p in paths], before)
        value = {**self.client.identity, 'content_key': '42'*32}
        self.client.identity_path.write_text(packed(value))
        changed = self.client.identity_path.read_bytes()
        result = worker.process(self.reset_request(), self.config)
        self.assertEqual(result['recovery_reset'], 0); self.assertIn('different backup keys', result['error'])
        self.assertEqual(self.client.identity_path.read_bytes(), changed)
        self.assertEqual([p.read_bytes() for p in paths[:2]], before[:2])

    def test_identity_or_credentials_changing_during_new_password_hashing_abort_reset(self):
        self.save(); self.lock_both(); path = app_lock.path(self.library); before = path.read_bytes()
        hash_password = vault.hash_password
        def changed(password, **kwargs):
            verifier = hash_password(password, **kwargs)
            self.client.identity_path.write_text(packed({**self.client.identity, 'content_key': '42'*32}))
            return verifier
        with patch.object(vault, 'hash_password', side_effect=changed):
            result = worker.process(self.reset_request(), self.config)
        self.assertEqual(result['recovery_reset'], 0); self.assertIn('changed', result['error'])
        self.assertEqual(path.read_bytes(), before)

    def test_missing_native_helper_or_hash_failure_never_changes_a_local_lock(self):
        self.save(); self.lock_both(); path = app_lock.path(self.library); before = path.read_bytes()
        with patch.object(recovery, 'crypt', side_effect=LibraryError('Recovery unavailable')):
            result = worker.process(self.reset_request(), self.config)
        self.assertEqual(result['recovery_reset'], 0)
        with patch.object(vault, 'hash_password', side_effect=LibraryError('Authentication unavailable')):
            result = worker.process(self.reset_request(), self.config)
        self.assertEqual(result['recovery_reset'], 0); self.assertEqual(path.read_bytes(), before)

    def test_recovery_cannot_escape_guest_scope_or_replace_unconfigured_locks(self):
        self.save()
        result = worker.process(self.reset_request(), self.config)
        self.assertEqual(result['recovery_reset'], 0); self.assertFalse(app_lock.path(self.library).exists())
        result = worker.process(self.reset_request('hidden'), self.config)
        self.assertEqual(result['recovery_reset'], 0); self.assertFalse(vault.configured(self.library))
        self.lock_both(); before = app_lock.path(self.library).read_bytes()
        source = self.folder/'Camera'; source.mkdir(); Image.new('RGB', (20, 20), 'blue').save(source/'photo.jpg')
        self.library.import_folders([source]); photo = self.library.query()['photos'][0]['id']
        app = app_lock.route(self.library, dict(action='app_lock_unlock', value='old local fixture password'))['app_lock_token']
        worker.process(dict(action='guest_start', ids=[photo], app_lock_token=app), self.config)
        result = worker.process(self.reset_request(), self.config)
        self.assertEqual(result['guest_active'], 1); self.assertNotIn('recovery_reset', result)
        self.assertEqual(app_lock.path(self.library).read_bytes(), before)

    def test_recovery_failures_share_bounded_attempts_and_never_weaken_password_rules(self):
        self.save(); self.lock_both(); before = app_lock.path(self.library).read_bytes()
        for _ in range(5):
            result = worker.process(self.reset_request(password='wrong recovery fixture password'), self.config)
            self.assertEqual(result['recovery_reset'], 0)
        result = worker.process(self.reset_request(), self.config)
        self.assertIn('Too many attempts', result['error']); self.assertEqual(app_lock.path(self.library).read_bytes(), before)
        access_sessions.clear_attempts(self.library, access_sessions.APP)
        result = worker.process(self.reset_request(replacement='1234'), self.config)
        self.assertEqual(result['recovery_reset'], 0); self.assertIn('8 characters', result['error'])
        self.assertEqual(app_lock.path(self.library).read_bytes(), before)

    def test_nonregular_recovery_and_linked_credential_files_are_refused(self):
        self.save(); self.lock_both(); target = app_lock.path(self.library)
        before = target.read_bytes(); external = self.folder/'external'; external.write_bytes(before)
        target.unlink(); target.symlink_to(external)
        result = worker.process(self.reset_request(), self.config)
        self.assertEqual(result['recovery_reset'], 0); self.assertEqual(external.read_bytes(), before)
        fifo = self.folder/'fifo'; os.mkfifo(fifo)
        with self.assertRaisesRegex(LibraryError, 'valid Photos recovery file'):
            recovery.read(self.library, self.config, str(fifo), PASSWORD)

    def test_recovery_and_normal_changes_never_follow_linked_lock_files(self):
        self.save(); self.lock_both()
        private = self.library.root/'private'; external = self.folder/'foreign-lock'
        external.write_bytes(b'foreign file'); external.chmod(0o640)
        for kind, name in [('app', '.app-lock.lock'), ('hidden', '.hidden-password.lock')]:
            lock = private/name; lock.unlink(missing_ok=True); lock.symlink_to(external)
            result = worker.process(self.reset_request(kind), self.config)
            self.assertEqual(result['recovery_reset'], 0); self.assertIn('safely', result['error'])
        result = app_lock.route(self.library, dict(action='app_lock_password', value='replacement fixture password'))
        self.assertIn('safely', result['error'])
        self.assertEqual(external.read_bytes(), b'foreign file'); self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o640)


if __name__ == '__main__': unittest.main()
