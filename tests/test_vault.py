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
        with patch('photos.vault.time.monotonic',return_value=time.monotonic()+601):
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
        with patch('photos.vault.time.monotonic',return_value=time.monotonic()+61):
            self.assertTrue(self.request('vault_unlock',value=self.password)['vault_unlocked'])
        for password in ['short','x'*1025,'nul\0password']:
            with self.assertRaises(LibraryError):self.request('vault_unlock',value=password)

    def test_hide_response_does_not_return_private_details_without_unlock(self):
        result=self.request('hidden',id=self.visible)
        self.assertNotIn('detail',result)
        self.assertNotIn(self.visible,[photo['id'] for photo in result['photos']])
        self.assertEqual(self.library.photo(self.visible)['hidden'],1)
