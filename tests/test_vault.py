import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from PIL import Image

from photos.library import Library, LibraryError, digest_file
from photos.worker import process
from photos import vault


class VaultTest(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.folder=Path(self.directory.name)
        self.source=self.folder/'source';self.source.mkdir()
        Image.new('RGB',(50,30),'red').save(self.source/'Hidden.png')
        Image.new('RGB',(30,50),'blue').save(self.source/'Visible.png')
        self.library=Library(self.folder/'library');self.library.import_folders([self.source])
        self.hidden=digest_file(self.source/'Hidden.png');self.visible=digest_file(self.source/'Visible.png')
        self.library.edit(self.hidden,'hidden')
        self.config=dict(root=str(self.library.root))
        self.password='Synthetic correct password'

    def tearDown(self):
        self.library.db.close();self.directory.cleanup()

    def request(self, action='query', **values):
        return process(dict(action=action,**values),self.config)

    def setup(self):
        return self.request('vault_setup',value=self.password)['vault_token']

    def test_locked_reads_originals_edits_exports_and_hidden_trash_are_denied(self):
        result=self.request(view='hidden')
        self.assertEqual(result['count'],0)
        self.assertFalse(result['vault_configured']);self.assertFalse(result['vault_unlocked'])
        self.assertEqual(result['photos'],[])
        before=self.library.photo(self.hidden)
        for action in ['open','caption','hidden','export','image_preview','image_save']:
            result=self.request(action,id=self.hidden,value='A changed value',view='hidden')
            self.assertIn('error',result)
            self.assertEqual(result['photos'],[])
            self.assertNotIn('detail',result)
        result=self.request('favorite_set',ids=[self.visible,self.hidden],value='1')
        self.assertIn('error',result)
        self.assertEqual(self.library.photo(self.visible)['favorite'],0)
        self.assertEqual(self.library.photo(self.hidden),before)
        self.library.edit(self.hidden,'trash')
        self.assertEqual(self.request(view='trash')['count'],0)
        self.assertEqual(self.library.query('trash')['count'],1)

    def test_native_password_verifier_unlock_lock_expiry_and_library_binding(self):
        original=self.library.original(self.library.photo(self.hidden)).read_bytes()
        token=self.setup()
        settings=self.library.root/'private/hidden-password.json'
        self.assertEqual(settings.stat().st_mode & 0o777,0o600)
        self.assertNotIn(self.password.encode(),settings.read_bytes())
        self.assertTrue(json.loads(settings.read_bytes())['verifier'].startswith('$argon2id$'))
        result=self.request(view='hidden',vault_token=token)
        self.assertEqual(result['count'],1);self.assertTrue(result['vault_unlocked'])
        result=self.request('open',id=self.hidden,view='hidden',vault_token=token)
        self.assertEqual(result['detail']['id'],self.hidden)
        self.request('caption',id=self.hidden,value='An authenticated edit',view='hidden',vault_token=token)
        self.assertEqual(self.library.photo(self.hidden)['caption'],'An authenticated edit')
        other=Library(self.folder/'other')
        try:self.assertFalse(vault.unlocked(other,token))
        finally:other.db.close()
        self.request('vault_lock',vault_token=token)
        self.assertFalse(vault.unlocked(self.library,token))
        with self.assertRaisesRegex(LibraryError,'Incorrect password'):
            self.request('vault_unlock',value='Another wrong password')
        token=self.request('vault_unlock',value=self.password)['vault_token']
        from photos import access_sessions
        shortened = access_sessions.Registry(session_ms=30)
        self.addCleanup(shortened.close)
        with patch('photos.access_sessions.registry',shortened):
            token=self.request('vault_unlock',value=self.password)['vault_token']
            time.sleep(.05)
            result=self.request('open',id=self.hidden,view='hidden',vault_token=token)
        self.assertIn('error',result);self.assertEqual(result['photos'],[])
        self.assertFalse(result['vault_unlocked'])
        self.assertEqual(self.library.original(self.library.photo(self.hidden)).read_bytes(),original)

    def test_confirmation_server_setup_cannot_overwrite_and_attempts_are_limited(self):
        self.setup()
        settings=self.library.root/'private/hidden-password.json'
        before=settings.read_bytes()
        with self.assertRaisesRegex(LibraryError,'already has'):
            self.request('vault_setup',value='A replacement password')
        self.assertEqual(settings.read_bytes(),before)
        for _ in range(5):
            with self.assertRaisesRegex(LibraryError,'Incorrect password'):
                self.request('vault_unlock',value='A wrong test password')
        with self.assertRaisesRegex(LibraryError,'Too many attempts'):
            self.request('vault_unlock',value=self.password)
        from photos import access_sessions
        access_sessions.clear_attempts(self.library, access_sessions.HIDDEN)
        self.assertTrue(self.request('vault_unlock',value=self.password)['vault_unlocked'])
        for password in ['short','x'*1025,'nul\0password']:
            with self.assertRaises(LibraryError):self.request('vault_unlock',value=password)

    def test_hide_response_does_not_return_private_details_without_unlock(self):
        result=self.request('hidden',id=self.visible)
        self.assertNotIn('detail',result)
        self.assertNotIn(self.visible,[photo['id'] for photo in result['photos']])
        self.assertEqual(self.library.photo(self.visible)['hidden'],1)

    def test_concurrent_hide_between_authorization_and_mutation_rolls_back_selection(self):
        def raced(library, request):
            self.library.edit(self.visible, 'hidden_set', '1')
            return False
        with patch('photos.worker.vault.protected', side_effect=raced):
            with self.assertRaisesRegex(LibraryError, 'Hidden is locked'):
                self.request('favorite_set', ids=[self.visible], value='1')
        self.assertEqual(self.library.photo(self.visible)['favorite'], 0)

    def test_photo_hidden_between_native_paging_and_row_read_exposes_no_paths(self):
        from photos.query import page
        def raced(library, **request):
            result = page(library, **request)
            self.library.edit(self.visible, 'hidden_set', '1')
            return result
        with patch('photos.query.page', side_effect=raced):
            result = self.request()
        self.assertEqual(result['photos'], [])

    def test_unlock_expiring_during_gallery_query_exposes_no_hidden_paths(self):
        token = self.setup()
        query = Library.query
        def expired(library, *args, **values):
            result = query(library, *args, **values)
            vault.lock(library, token)
            return result
        with patch.object(Library, 'query', expired):
            result = self.request(view='hidden', vault_token=token)
        self.assertEqual(result['photos'], [])
        self.assertEqual(result['count'], 0)
        self.assertFalse(result['vault_unlocked'])

    def test_password_change_revokes_all_sessions_and_preserves_originals_and_keys(self):
        token = self.setup()
        other_token = self.request('vault_unlock', value=self.password)['vault_token']
        original = self.library.original(self.library.photo(self.hidden)).read_bytes()
        identity = self.library.root / 'private/identity.json'
        identity.write_bytes(b'Synthetic account and content keys')
        new_password = 'Synthetic replacement password'
        result = self.request('vault_change', value=new_password, vault_token=token)
        self.assertFalse(result['vault_unlocked'])
        self.assertEqual(result['photos'], [])
        self.assertFalse(vault.unlocked(self.library, token))
        self.assertFalse(vault.unlocked(self.library, other_token))
        self.assertNotIn(new_password, json.dumps(result))
        settings = self.library.root / 'private/hidden-password.json'
        self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(new_password.encode(), settings.read_bytes())
        with self.assertRaisesRegex(LibraryError, 'Incorrect password'):
            self.request('vault_unlock', value=self.password)
        self.assertTrue(self.request('vault_unlock', value=new_password)['vault_unlocked'])
        self.assertEqual(identity.read_bytes(), b'Synthetic account and content keys')
        self.assertEqual(self.library.original(self.library.photo(self.hidden)).read_bytes(), original)

    def test_password_change_rejects_locked_expired_or_failed_hash_without_replacing_password(self):
        token = self.setup()
        settings = self.library.root / 'private/hidden-password.json'
        before = settings.read_bytes()
        for candidate in ['', 'not a session', token]:
            password = 'short' if candidate == token else 'Synthetic replacement password'
            with self.assertRaises(LibraryError):
                self.request('vault_change', value=password, vault_token=candidate)
            self.assertEqual(settings.read_bytes(), before)
        with patch('photos.vault.verify_password', side_effect=LibraryError('Hidden authentication is unavailable')):
            with self.assertRaises(LibraryError):
                self.request('vault_change', value='Synthetic replacement password', vault_token=token)
        self.assertEqual(settings.read_bytes(), before)
        def expire(password):
            vault.lock(self.library, token)
            return json.loads(before)['verifier']
        with patch('photos.vault.hash_password', side_effect=expire):
            with self.assertRaisesRegex(LibraryError, 'Unlock Hidden'):
                self.request('vault_change', value='Synthetic replacement password', vault_token=token)
        self.assertEqual(settings.read_bytes(), before)
        self.assertTrue(self.request('vault_unlock', value=self.password)['vault_unlocked'])
