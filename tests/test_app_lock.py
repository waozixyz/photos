import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from photos import app_lock, guest, vault
from photos.library import Library, digest_file
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
        self.addCleanup(vault.failures.pop, str(self.library.root), None)
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
        for action in ['query', 'open', 'favorite', 'hidden', 'trash', 'export', 'import', 'sync',
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
        token = self.unlock(); entry = app_lock.sessions[token]
        app_lock.sessions[token] = (entry[0], 0, entry[2])
        self.assertEqual(self.request(action='app_lock_check', app_lock_token=token)['app_locked'], 1)
        token = self.unlock()
        hidden = vault.authenticate(self.library, self.password)
        vault.change_password(self.library, hidden, 'replacement-private-password')
        self.assertEqual(self.request(action='query', app_lock_token=token)['app_locked'], 1)
        self.password = 'replacement-private-password'; self.unlock()

    def test_expiry_during_owner_operation_returns_no_photo_or_derived_path(self):
        self.enable(); token = self.unlock()
        import photos.worker as worker
        original = worker.library_request
        def expire(library, request, config):
            result = original(library, request, config)
            entry = app_lock.sessions[token]; app_lock.sessions[token] = (entry[0], 0, entry[2])
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
