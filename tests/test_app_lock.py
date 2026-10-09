import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image
from photos import app_lock, guest, vault, access_sessions
from photos.library import Library, LibraryError, digest_file
from photos.worker import process
from photos.sync import Client


class AppLockTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.source = self.root/'source/Family'; self.source.mkdir(parents=True)
        photo = self.source/'Photo.png'; Image.new('RGB', (40, 30), '#459a73').save(photo)
        self.original = photo.read_bytes(); self.identity = digest_file(photo)
        self.library = Library(self.root/'library'); self.addCleanup(self.library.db.close)
        self.library.import_folders([self.source.parent]); self.config = dict(root=str(self.library.root))
        Client(self.library, dict(node='http://127.0.0.1:1', node_id='42'*32))
        self.password = 'private-app-lock-password'
        self.hidden = vault.authenticate(self.library, self.password, setup=True)
        self.addCleanup(app_lock.revoke, self.library)
        self.addCleanup(access_sessions.clear_attempts, self.library, access_sessions.APP)
        self.addCleanup(access_sessions.clear_attempts, self.library, access_sessions.HIDDEN)
        self.addCleanup(vault.lock, self.library, self.hidden)

    def request(self, **values):
        return process(dict(gallery_format=2, **values), self.config)

    def enable(self):
        result = self.request(action='app_lock_enable', value=self.password)
        self.assertEqual(result['app_locked'], 1)
        self.assertFalse(vault.unlocked(self.library, self.hidden))

    def unlock(self):
        result = self.request(action='app_lock_unlock', value=self.password)
        self.assertEqual(result['app_locked'], 0)
        self.assertFalse(result['vault_unlocked'])
        return result['app_lock_token']

    def test_opening_and_all_owner_actions_require_a_separate_app_session(self):
        self.enable()
        for action in ['query', 'open', 'favorite', 'hidden', 'trash', 'export', 'import', 'sync', 'swipe_selection',
                       'playback_check', 'vault_unlock', 'guest_start', 'app_lock_disable']:
            result = self.request(action=action, id=self.identity, value=self.password, vault_token=self.hidden)
            self.assertEqual(result['app_locked'], 1); self.assertFalse(result['photos']); self.assertFalse(result['albums'])
            self.assertNotIn('playback_path', result)
        token = self.unlock()
        opened = self.request(action='open', id=self.identity, app_lock_token=token)
        self.assertEqual(opened['detail']['id'], self.identity)
        self.assertFalse(opened['vault_unlocked'])
        self.assertEqual((self.source/'Photo.png').read_bytes(), self.original)

    def test_wrong_password_disable_and_manual_lock_preserve_originals_and_keys(self):
        key = (self.library.root/'private/identity.json').read_bytes()
        self.enable(); marker = app_lock.path(self.library).read_bytes()
        failed = self.request(action='app_lock_unlock', value='incorrect-password')
        self.assertIn('Incorrect password', failed['error']); self.assertEqual(failed['app_locked'], 1)
        self.assertEqual(app_lock.path(self.library).read_bytes(), marker)
        token = self.unlock()
        locked = self.request(action='app_lock_lock')
        self.assertEqual(locked['app_locked'], 1)
        self.assertEqual(self.request(action='query', app_lock_token=token)['app_locked'], 1)
        token = self.unlock()
        disabled = self.request(action='app_lock_disable', value=self.password, app_lock_token=token)
        self.assertEqual(disabled['app_lock_enabled'], 0); self.assertEqual(disabled['total'], 1)
        self.assertEqual(len(disabled['photos']), 1)
        self.assertEqual((self.library.root/'private/identity.json').read_bytes(), key)
        self.assertEqual((self.source/'Photo.png').read_bytes(), self.original)

    def test_service_restart_expired_sessions_and_password_changes_relock(self):
        self.enable(); token = self.unlock()
        app_lock.revoke(self.library)
        self.assertEqual(self.request(action='query', app_lock_token=token)['app_locked'], 1)
        shortened = access_sessions.Registry(session_ms=30); self.addCleanup(shortened.close)
        with patch('photos.access_sessions.registry',shortened):
            token = self.unlock(); time.sleep(.05)
            self.assertEqual(self.request(action='app_lock_check', app_lock_token=token)['app_locked'], 1)
        token = self.unlock()
        hidden = vault.authenticate(self.library, self.password)
        vault.change_password(self.library, hidden, 'replacement-private-password')
        self.assertEqual(self.request(action='query', app_lock_token=token)['app_locked'], 1)
        self.password = 'replacement-private-password'; self.unlock()

    def test_access_ending_during_owner_operation_returns_no_photo_or_derived_path(self):
        self.enable(); token = self.unlock()
        import photos.worker as worker
        original = worker.library_request
        def expire(library, request, config):
            result = original(library, request, config)
            access_sessions.lock(library, access_sessions.APP, token)
            return result
        with patch.object(worker, 'library_request', expire):
            result = self.request(action='open', id=self.identity, app_lock_token=token)
        self.assertEqual(result['app_locked'], 1); self.assertFalse(result['photos'])
        self.assertNotIn('detail', result)

    def test_guest_exit_authenticates_app_but_never_unlocks_hidden(self):
        self.enable(); token = self.unlock()
        result = self.request(action='guest_start', id=self.identity, app_lock_token=token)
        self.assertEqual(result['guest_active'], 1); self.assertFalse(app_lock.unlocked(self.library, token))
        self.assertEqual(self.request(action='query')['guest_active'], 1)
        result = self.request(action='guest_exit', value=self.password)
        self.assertEqual(result['guest_exit_authorized'], 1); self.assertEqual(result['app_locked'], 0)
        self.assertFalse(result['vault_unlocked']); self.assertFalse(guest.path(self.library).exists())
        self.assertEqual(self.request(action='query', app_lock_token=result['app_lock_token'])['total'], 1)

    def test_malformed_and_linked_lock_settings_fail_closed_and_cannot_follow_target(self):
        outside = self.root/'outside.json'; outside.write_text('Unrelated private data')
        for raw in ['{', '{}', 'null', json.dumps(dict(version=1, enabled=True)),
                    json.dumps(dict(version=True, enabled=False)), json.dumps(dict(version=1, enabled=0))]:
            app_lock.path(self.library).write_text(raw)
            self.assertEqual(self.request(action='query')['app_locked'], 1)
            token = self.unlock()
            self.assertEqual(self.request(action='app_lock_disable', value=self.password, app_lock_token=token)['app_lock_enabled'], 0)
        app_lock.path(self.library).unlink(); app_lock.path(self.library).symlink_to(outside)
        self.assertEqual(self.request(action='query')['app_locked'], 1)
        token = self.unlock()
        self.request(action='app_lock_disable', value=self.password, app_lock_token=token)
        self.assertEqual(outside.read_text(), 'Unrelated private data')

    def test_separate_pin_works_without_hidden_keeps_leading_zeroes_and_guest_exit(self):
        (self.library.root/'private/hidden-password.json').unlink()
        key = (self.library.root/'private/identity.json').read_bytes()
        result = self.request(action='app_lock_pin', value='0042')
        self.assertEqual(result['app_locked'], 1); self.assertEqual(result['app_lock_method'], 1)
        self.assertNotIn(b'0042', app_lock.path(self.library).read_bytes())
        self.assertEqual(app_lock.path(self.library).stat().st_mode & 0o777, 0o600)
        result = self.request(action='app_lock_unlock', value='0042')
        self.assertEqual(result['app_locked'], 0); self.assertFalse(result['vault_unlocked'])
        self.assertTrue(result['guest_configured']); self.assertFalse(result['vault_configured'])
        token = result['app_lock_token']
        self.assertEqual(self.request(action='guest_start', id=self.identity, app_lock_token=token)['guest_active'], 1)
        self.assertIn('Incorrect PIN', self.request(action='guest_exit', value='1234')['error'])
        result = self.request(action='guest_exit', value='0042')
        self.assertEqual(result['guest_exit_authorized'], 1); self.assertEqual(result['app_locked'], 0)
        self.assertFalse(result['vault_unlocked']); self.assertEqual(result['app_lock_method'], 1)
        self.assertEqual(self.request(action='app_lock_disable', value='0042', app_lock_token=result['app_lock_token'])['total'], 1)
        self.assertFalse(vault.configured(self.library))
        self.assertEqual((self.library.root/'private/identity.json').read_bytes(), key)
        self.assertEqual((self.source/'Photo.png').read_bytes(), self.original)

    def test_separate_password_and_method_changes_revoke_sessions_without_changing_hidden(self):
        marker = (self.library.root/'private/hidden-password.json').read_bytes()
        result = self.request(action='app_lock_password', value='Separate app password')
        self.assertEqual(result['app_lock_method'], 2)
        self.assertIn('Incorrect password', self.request(action='app_lock_unlock', value=self.password)['error'])
        result = self.request(action='app_lock_unlock', value='Separate app password'); token = result['app_lock_token']
        self.assertEqual(self.request(action='app_lock_pin', value='0067')['app_locked'], 1)
        result = self.request(action='app_lock_pin', value='0067', app_lock_token=token)
        self.assertEqual(result['app_locked'], 1); self.assertEqual(result['app_lock_method'], 1)
        self.assertFalse(app_lock.unlocked(self.library, token))
        token = self.request(action='app_lock_unlock', value='0067')['app_lock_token']
        result = self.request(action='app_lock_enable', value=self.password, app_lock_token=token)
        self.assertEqual(result['app_lock_method'], 0); self.assertEqual(result['app_locked'], 1)
        self.assertFalse(app_lock.unlocked(self.library, token)); self.unlock()
        self.assertEqual((self.library.root/'private/hidden-password.json').read_bytes(), marker)

    def test_invalid_credentials_rate_limit_and_failed_hash_never_replace_the_active_lock(self):
        for value in ['123', '1'*13, '123a', '１２３４', None, True]:
            result = self.request(action='app_lock_pin', value=value)
            self.assertIn('error', result); self.assertFalse(app_lock.enabled(self.library))
        for value in ['short', 'x'*1025, 'invalid\0password']:
            self.assertIn('error', self.request(action='app_lock_password', value=value))
        self.request(action='app_lock_pin', value='0000')
        marker = app_lock.path(self.library).read_bytes()
        for _ in range(5): self.assertIn('Incorrect PIN', self.request(action='app_lock_unlock', value='1111')['error'])
        self.assertIn('Too many attempts', self.request(action='app_lock_unlock', value='0000')['error'])
        access_sessions.clear_attempts(self.library, access_sessions.APP)
        token = self.request(action='app_lock_unlock', value='0000')['app_lock_token']
        with patch('photos.vault.hash_password', side_effect=LibraryError('App authentication could not finish')):
            self.assertIn('error', self.request(action='app_lock_password', value='New app password', app_lock_token=token))
        self.assertEqual(app_lock.path(self.library).read_bytes(), marker)

    def test_access_ending_during_password_hashing_preserves_the_old_credential(self):
        self.enable(); token = self.unlock(); marker = app_lock.path(self.library).read_bytes()
        original = vault.hash_password
        def expire(password, **options):
            verifier = original(password, **options)
            access_sessions.lock(self.library, access_sessions.APP, token)
            return verifier
        with patch('photos.vault.hash_password', expire):
            result = self.request(action='app_lock_password', value='New app password', app_lock_token=token)
        self.assertEqual(result['app_locked'], 1); self.assertIn('App access ended', result['error'])
        self.assertEqual(app_lock.path(self.library).read_bytes(), marker); self.unlock()

    def test_native_pin_modes_are_bounded_and_do_not_weaken_hidden_password_rules(self):
        import struct, subprocess
        package = Path(vault.__file__).resolve().parents[1]
        program = next(p for p in [package/'vault_auth', package/'build/vault-auth/vault_auth'] if p.is_file())
        verifier = vault.hash_password('0042', pin=True)
        self.assertEqual(vault.verify_password('0042', verifier, pin=True).returncode, 0)
        for action, value in [('hash', b'0042'), ('pin-hash', '１２３４'.encode()), ('pin-hash', b'123'), ('pin-hash', b'1234567890123')]:
            # All native failures remain silent; the secret never enters argv.
            result = subprocess.run([str(program), action], input=struct.pack('!I',len(value))+value, capture_output=True)
            self.assertEqual(result.returncode, 2); self.assertFalse(result.stdout); self.assertFalse(result.stderr)
        with self.assertRaises(LibraryError): vault.authenticate(self.library, '0042')

    def test_background_delay_is_authenticated_bounded_local_and_preserves_credentials(self):
        self.enable(); token = self.unlock()
        original = app_lock.path(self.library).read_bytes()
        self.assertEqual(self.request(action='app_lock_delay', value='5')['app_locked'], 1)
        self.assertEqual(app_lock.path(self.library).read_bytes(), original)
        for delay in ['-1','1','600','999999',None,True,[],{}]:
            result = self.request(action='app_lock_delay', value=delay, app_lock_token=token)
            self.assertIn('error', result); self.assertEqual(app_lock.path(self.library).read_bytes(), original)
        for delay in [5,15,60,300,1800,0]:
            result = self.request(action='app_lock_delay', value=str(delay), app_lock_token=token)
            self.assertEqual(result['app_lock_delay'], delay); self.assertFalse(result['app_locked'])
            self.assertNotIn('photos', result); self.assertFalse(app_lock.unlocked(self.library, token))
            token = result['app_lock_token']; self.assertTrue(app_lock.unlocked(self.library, token))
            original = app_lock.path(self.library).read_bytes()
        self.request(action='app_lock_delay', value='15', app_lock_token=token)
        app_lock.revoke(self.library)
        result = self.request(action='query')
        self.assertEqual(result['app_lock_delay'], 15); self.assertEqual(result['app_locked'], 1)
        token = self.unlock()
        self.request(action='app_lock_pin', value='0042', app_lock_token=token)
        result = self.request(action='app_lock_unlock', value='0042')
        self.assertEqual(result['app_lock_delay'], 15)
        self.assertEqual((self.source/'Photo.png').read_bytes(), self.original)
