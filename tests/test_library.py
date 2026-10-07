import json
from pathlib import Path
import tempfile
import unittest
from PIL import Image
from photos.library import Library, digest_file
from photos.sync import Client, encrypt, decrypt
from unittest.mock import Mock
import urllib.error


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        self.library = Library(self.root / 'library')
        self.addCleanup(self.library.db.close)

    def export(self, base, album, color='red', metadata=True):
        path = self.root / base / album / 'same.jpg'
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (100, 80), color).save(path)
        if metadata:
            sidecar = path.parent / ('metadata' if base == 'desktop' else '.meta') / 'same.jpg.json'
            sidecar.parent.mkdir()
            sidecar.write_text(json.dumps(dict(description='A preserved caption', photoTakenTime={'timestamp': '1600000000'}, geoData={'latitude': 1.5})))
        return path

    def test_merge_deduplicates_preserves_albums_dates_metadata_and_original(self):
        first = self.export('desktop', 'Holiday')
        second = self.export('cli', 'Favorites')
        before = first.read_bytes()
        result = self.library.import_folders([first.parents[1], second.parents[1]])
        self.assertEqual(result['added'], 1)
        self.assertEqual(result['merged'], 1)
        photo = self.library.photo(digest_file(first))
        self.assertEqual(json.loads(photo['albums']), ['Favorites', 'Holiday'])
        self.assertEqual(photo['taken'], 1600000000)
        self.assertEqual(photo['caption'], 'A preserved caption')
        self.assertEqual(json.loads(photo['metadata'])['geoData']['latitude'], 1.5)
        self.assertEqual(before, self.library.original(photo).read_bytes())
        self.assertEqual(before, first.read_bytes())
        self.assertEqual(self.library.import_folders([first.parents[1]])['unchanged'], 1)

    def test_gallery_response_is_small_and_keeps_newest_dates_first(self):
        from photos.worker import library_request
        first = self.export('desktop', 'Old', 'red')
        second = self.export('desktop', 'New', 'blue')
        self.library.import_folders([first.parents[1]])
        self.library.db.execute('UPDATE photos SET taken=? WHERE id=?', (1700000000, digest_file(second)))
        self.library.db.commit()
        response = library_request(self.library, {}, {})
        self.assertEqual(response['photos'][0]['id'], digest_file(second))
        self.assertGreater(response['photos'][0]['date'], response['photos'][1]['date'])
        self.assertNotIn('metadata', response['photos'][0])
        self.assertNotIn('manifest', response['photos'][0])
        self.assertEqual(self.library.photo(digest_file(second))['caption'], 'A preserved caption')

    def test_identical_names_with_different_bytes_stay_separate(self):
        first = self.export('desktop', 'Holiday', 'red')
        second = self.export('cli', 'Holiday', 'blue')
        self.library.import_folders([first.parents[1], second.parents[1]])
        self.assertEqual(self.library.query()['total'], 2)

    def test_export_corrects_the_date_of_an_existing_phone_copy(self):
        phone = self.export('phone', 'Camera', metadata=False)
        exported = self.export('desktop', 'Holiday')
        self.library.import_folders([phone.parents[1], exported.parents[1]])
        self.assertEqual(self.library.photo(digest_file(phone))['taken'], 1600000000)

    def test_library_cannot_import_itself_or_its_originals(self):
        original = self.library.root / 'originals'
        original.mkdir()
        for folder in [self.library.root, original]:
            with self.assertRaises(ValueError):
                self.library.import_folders([folder])

    def test_trash_restore_hide_search_and_user_edits_survive_rescan(self):
        path = self.export('desktop', 'Holiday')
        self.library.import_folders([path.parents[1]])
        photo_id = digest_file(path)
        self.library.edit(photo_id, 'caption', 'My own caption')
        self.library.edit(photo_id, 'tags', 'forest hiking')
        self.library.edit(photo_id, 'favorite')
        self.assertEqual(self.library.query('favorites', 'hiking')['count'], 1)
        self.library.edit(photo_id, 'trash')
        self.assertEqual(self.library.query()['count'], 0)
        self.assertEqual(self.library.query('trash')['count'], 1)
        self.assertTrue(self.library.original(self.library.photo(photo_id)).exists())
        self.library.edit(photo_id, 'restore')
        self.library.edit(photo_id, 'hidden')
        self.assertEqual(self.library.query()['count'], 0)
        self.assertEqual(self.library.query('hidden')['count'], 1)
        self.library.import_folders([path.parents[1]])
        self.assertEqual(self.library.photo(photo_id)['caption'], 'My own caption')

    def test_bad_metadata_does_not_drop_an_original_or_import_other_files(self):
        path = self.export('desktop', 'Holiday')
        (path.parent / 'metadata/same.jpg.json').write_text('{')
        (path.parent / 'credential.txt').write_text('not a photo')
        (path.parent / 'link.jpg').symlink_to(path)
        result = self.library.import_folders([path.parents[1]])
        self.assertEqual(result['warning'], 1)
        self.assertEqual(self.library.query()['total'], 1)

    def test_export_preserves_metadata_and_refuses_overwrite(self):
        path = self.export('desktop', 'Holiday')
        self.library.import_folders([path.parents[1]])
        photo_id = digest_file(path)
        destination = Path(self.library.export(photo_id, self.root / 'export'))
        self.assertEqual(destination.read_bytes(), path.read_bytes())
        self.assertTrue((destination.parent / 'metadata' / (destination.name + '.json')).exists())

    def test_public_urls_and_unpinned_nodes_are_rejected_before_key_creation(self):
        for node in ['https://api.waozi.xyz', 'http://thinkpad.local:18080', 'http://192.168.100.97:18080', 'http://127.0.0.1:18080/path', 'http://127.0.0.1:18080']:
            with self.assertRaises(ValueError):
                Client(self.library, dict(node=node, node_id=''))
        self.assertFalse((self.library.root / 'private/identity.json').exists())

    def test_metadata_encryption_is_bound_to_account_collection_and_id(self):
        path = self.export('desktop', 'Holiday')
        self.library.import_folders([path.parents[1]])
        client = Client(self.library, dict(node='http://127.0.0.1:18080', node_id='a' * 64))
        client.upload_original = lambda photo: dict(chunks=['b' * 64])
        photo = self.library.photo(digest_file(path))
        record = client.record(photo)
        self.assertNotIn('caption', record['ciphertext'])
        record['id'] = 'f' * 64
        with self.assertRaises(ValueError):
            client.accept(record)

    def test_sync_refuses_wrong_identity_and_outward_photos_replication(self):
        client = Client(self.library, dict(node='http://127.0.0.1:18080', node_id='a' * 64))
        client.raw = Mock(return_value=dict(node_id='b' * 64))
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            client.check_node()
        client.raw.return_value = dict(node_id='a' * 64, known_nodes=[
            dict(sync=dict(direction='push', apps=['photos']))])
        with self.assertRaisesRegex(ValueError, 'outward Photos replication'):
            client.check_node()
        client.raw.return_value['known_nodes'][0]['sync'] = dict(direction='pull', apps=['inbe'])
        client.check_node()

    def test_failed_batch_stays_queued_and_edits_during_upload_are_retained(self):
        path = self.export('desktop', 'Holiday')
        self.library.import_folders([path.parents[1]])
        photo_id = digest_file(path)
        client = Client(self.library, dict(node='http://127.0.0.1:18080', node_id='a' * 64))
        client.check_node = Mock()
        client.prepare_original = Mock(return_value=dict(chunks=['b' * 64]))
        client.record = Mock(return_value=dict(id=photo_id))
        client.exchange = Mock(side_effect=[{}, dict(status='error')])
        with self.assertRaises(ValueError):
            client.sync()
        self.assertEqual(self.library.photo(photo_id)['dirty'], 1)

        def exchange(records=None):
            if records:
                self.library.edit(photo_id, 'caption', 'Edited during upload')
            return dict(status='ok')
        client.exchange = exchange
        self.assertEqual(client.sync(), 1)
        self.assertEqual(self.library.photo(photo_id)['caption'], 'Edited during upload')

    def test_changed_chunk_bytes_never_reuse_an_encryption_nonce(self):
        client = Client(self.library, dict(node='http://127.0.0.1:18080', node_id='a' * 64))
        uploaded = []
        def request(path, method='GET', body=None, binary=False):
            if method == 'HEAD':
                raise urllib.error.HTTPError(path, 404, 'missing', {}, None)
            uploaded.append(body)
        client.request = request
        for plain in [b'original', b'changed', b'original']:
            client.upload_blob(plain, 'b' * 64, 'chunk:0')
        self.assertNotEqual(uploaded[0][:24], uploaded[1][:24])
        self.assertEqual(uploaded[0], uploaded[2])


if __name__ == '__main__':
    unittest.main()
