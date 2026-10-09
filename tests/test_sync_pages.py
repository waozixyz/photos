import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from PIL import Image
from photos.library import Library, digest_file
from photos.sync import Client, COLLECTION, ALBUM_COLLECTION


class SyncPagesTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.library = Library(root / 'library')
        self.addCleanup(self.library.db.close)
        self.client = Client(self.library, dict(node='http://127.0.0.1:18080', node_id='a' * 64))
        source = root / 'Camera'
        source.mkdir()
        self.records = []
        for index, color in enumerate(['red', 'blue']):
            path = source / f'{index}.jpg'
            Image.new('RGB', (100, 80), color).save(path)
        self.library.import_folders([source])
        for path in sorted(source.iterdir()):
            self.records.append(self.client.record(self.library.photo(digest_file(path)), dict(chunks=[])))
        self.library.db.execute('DELETE FROM photos')
        self.library.db.commit()

    def page(self, record, complete, version=10, cursor_version=5):
        result = dict(status='ok', encrypted_records_only=True, server_version=version,
                      changes_complete=complete, changes=dict(encrypted_records=[record]))
        if not complete:
            result['record_cursor'] = dict(version=cursor_version, collection=COLLECTION, id=record['id'])
        return result

    def test_failed_following_page_resumes_without_resending_uploads(self):
        first = self.page(self.records[0], False)
        self.client.request = Mock(side_effect=[first, OSError('offline')])
        with self.assertRaises(OSError):
            self.client.exchange([dict(id='upload-fixture')])
        self.assertEqual(self.library.query()['total'], 1)
        self.assertEqual(self.library.setting('server_version', 0), 0)
        self.assertEqual(self.library.setting('record_cursor'), first['record_cursor'])
        requests = [json.loads(call.args[2]) for call in self.client.request.call_args_list]
        self.assertEqual(requests[0]['encrypted_records'], [dict(id='upload-fixture')])
        self.assertEqual(requests[1]['encrypted_records'], [])
        self.client.request = Mock(return_value=self.page(self.records[1], True))
        self.client.exchange()
        self.assertEqual(self.library.query()['total'], 2)
        self.assertEqual(self.library.setting('server_version'), 10)
        self.assertIsNone(self.library.setting('record_cursor'))
        self.assertEqual(json.loads(self.client.request.call_args.args[2])['record_cursor'], first['record_cursor'])

    def test_decryption_failure_rolls_back_the_whole_page(self):
        invalid = {**self.records[1], 'nonce': 'tampered'}
        result = self.page(self.records[0], True)
        result['changes']['encrypted_records'].append(invalid)
        self.client.request = Mock(return_value=result)
        with self.assertRaises(ValueError):
            self.client.exchange()
        self.assertEqual(self.library.query()['total'], 0)
        self.assertEqual(self.library.setting('server_version', 0), 0)
        self.assertIsNone(self.library.setting('record_cursor'))

    def test_stalled_conflicting_or_unrecognized_pages_keep_checkpoint(self):
        initial = self.page(self.records[0], False)
        self.library.set_setting('record_cursor', initial['record_cursor'])
        self.library.set_setting('server_version', 1)
        responses = [initial, {**initial, 'changes_complete': True},
                     {**initial, 'encrypted_records_only': False},
                     {**initial, 'server_version': 0},
                     {**initial, 'record_cursor': dict(version=6, collection=COLLECTION, id='other')}]
        for result in responses:
            with self.subTest(result=result):
                self.client.request = Mock(return_value=result)
                with self.assertRaises(ValueError):
                    self.client.exchange()
                self.assertEqual(self.library.setting('record_cursor'), initial['record_cursor'])
                self.assertEqual(self.library.setting('server_version'), 1)
                self.assertEqual(self.library.query()['total'], 0)

    def test_empty_final_page_advances_high_watermark(self):
        self.client.request = Mock(return_value=dict(status='ok', encrypted_records_only=True,
            changes_complete=True, server_version=100, changes=dict(encrypted_records=[])))
        self.client.exchange()
        self.assertEqual(self.library.setting('server_version'), 100)

    def test_album_metadata_encryption_is_bound_and_page_failure_preserves_local_edit(self):
        self.library.edit_album('', 'create', 'An empty album')
        self.library.edit_album('An empty album', 'description', 'Private description')
        record = self.client.album_record(self.library.album_details('An empty album'))
        self.assertEqual(record['collection'], ALBUM_COLLECTION)
        self.assertNotIn('Private description', json.dumps(record))
        self.library.edit_album('An empty album', 'pinned')
        self.client.accept(record)
        self.assertEqual(self.library.album_details('An empty album')['pinned'], 1)
        for changed in [{'collection': COLLECTION}, {'id': 'f' * 64}, {'nonce': 'tampered'}]:
            with self.subTest(changed=changed):
                response = self.page(self.records[0], True)
                response['changes']['encrypted_records'].append({**record, **changed})
                self.client.request = Mock(return_value=response)
                with self.assertRaises(ValueError):
                    self.client.exchange()
                self.assertEqual(self.library.query()['total'], 0)
                self.assertEqual(self.library.album_details('An empty album')['pinned'], 1)
                self.assertEqual(self.library.setting('server_version', 0), 0)


if __name__ == '__main__':
    unittest.main()
