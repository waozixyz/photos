import json
import base64
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from PIL import Image
from photos.library import Library, digest_file
from photos.sync import Client, COLLECTION, ALBUM_COLLECTION
from photos import sync_pages, semantic, people


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
        self.client.request = Mock(return_value=dict(status='ok', encrypted_records_only=True,
            changes_complete=True, server_version=101, changes=dict(encrypted_records=None)))
        self.client.exchange()
        self.assertEqual(self.library.setting('server_version'), 101)

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

    def test_native_write_failure_rolls_back_photos_albums_indexes_and_checkpoint(self):
        photo = self.client.open_record(self.records[0])
        vector = base64.b64encode(struct.pack('<512f', 1., *([0.]*511))).decode()
        photo['semantic'] = dict(model=semantic.MODEL, vector=vector)
        photo['people'] = dict(model=people.MODEL, faces=[], scanned=False, tags=[
            dict(id='e'*64, name='Private fixture', ignored=0, updated=1, birth='')])
        self.library.edit_album('', 'create', 'A remote album')
        album = self.client.album_record(self.library.album_details('A remote album'))
        self.library.db.execute('DELETE FROM albums'); self.library.db.commit()
        bad = self.client.open_record(self.records[1]); bad['size'] = 'bad'
        response = self.page(self.records[0], True)
        response['changes']['encrypted_records'] = [self.records[0], album, self.records[1]]
        with self.assertRaisesRegex(ValueError, 'Invalid photo metadata'):
            sync_pages.apply(self.library, response, 0, None,
                             [photo, self.client.open_record(album), bad], 200)
        for table in ['photos', 'albums', 'semantic_embeddings', 'people', 'person_tags']:
            self.assertEqual(self.library.db.execute('SELECT count(*) FROM '+table).fetchone()[0], 0)
        self.assertEqual(self.library.setting('server_version', 0), 0)
        self.assertIsNone(self.library.setting('record_cursor'))

    def test_native_merge_keeps_newer_local_edits_and_exact_unknown_metadata(self):
        photo = self.client.open_record(self.records[0])
        photo['metadata'] = dict(camera={'integer': 10**100, 'values': [None, True, '日本語\n\0']})
        record = self.client.seal_record(photo, COLLECTION, 'photos.media.v1')
        self.client.request = Mock(return_value=self.page(record, True))
        self.client.exchange()
        self.assertEqual(json.loads(self.library.photo(photo['id'])['metadata']), photo['metadata'])
        self.library.edit(photo['id'], 'caption', 'Newer local edit')
        before = self.library.photo(photo['id'])
        self.client.request = Mock(return_value=self.page(record, True, version=11))
        self.client.exchange()
        self.assertEqual(self.library.photo(photo['id']), before)
        self.assertEqual(before['dirty'], 1)
        self.assertEqual(self.library.setting('server_version'), 11)

    def test_native_checkpoint_rejects_stale_writer_snapshot_and_invalid_pages(self):
        response = self.page(self.records[0], True)
        decoded = [self.client.open_record(self.records[0])]
        self.library.set_setting('server_version', 5)
        with self.assertRaisesRegex(ValueError, 'stale'):
            sync_pages.apply(self.library, response, 0, None, decoded, 200)
        self.assertEqual(self.library.setting('server_version'), 5)
        self.assertEqual(self.library.query()['total'], 0)
        self.library.set_setting('server_version', 0)
        for changed in [dict(server_version=True), dict(server_version=2**63),
                        dict(changes_complete=None), dict(full_snapshot_required=True),
                        dict(record_cursor={'version': 4}), dict(status='success')]:
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(ValueError, 'cursor was retained'):
                    sync_pages.apply(self.library, {**response, **changed}, 0, None, decoded, 200)
                self.assertEqual(self.library.query()['total'], 0)
        with self.assertRaises(ValueError):
            sync_pages.apply(self.library, response, 0, None, [], 200)
        with self.assertRaises(ValueError):
            sync_pages.apply(self.library, response, 0, None, [json.dumps(decoded[0])], 200)
        with self.assertRaises(ValueError):
            changed = {**response, 'changes': {'encrypted_records': [json.dumps(self.records[0])]}}
            sync_pages.apply(self.library, changed, 0, None, decoded, 200)
        self.assertEqual(self.library.setting('server_version'), 0)

    def test_native_checkpoint_retains_exact_counters_above_float_precision(self):
        high = 2**53+1
        response = dict(status='ok', encrypted_records_only=True, changes_complete=True,
                        server_version=high, changes=dict(encrypted_records=[]))
        sync_pages.apply(self.library, response, 0, None, [], 200)
        self.assertEqual(self.library.setting('server_version'), high)
        sync_pages.apply(self.library, {**response, 'server_version': high+2}, high, None, [], 200)
        self.assertEqual(self.library.setting('server_version'), high+2)

    def test_native_album_conflict_retains_every_local_row_and_cursor(self):
        self.library.edit_album('', 'create', 'Conflicting name')
        local = self.library.album_details('Conflicting name')
        other = {**local, 'id': 'c'*64, 'updated': local['updated']+100, 'auto_people': []}
        record = self.client.seal_record(other, ALBUM_COLLECTION, 'photos.album.v1')
        response = self.page(self.records[0], True)
        response['changes']['encrypted_records'].append(record)
        with self.assertRaisesRegex(ValueError, 'Album name conflict'):
            sync_pages.apply(self.library, response, 0, None,
                             [self.client.open_record(self.records[0]), other], 200)
        self.assertEqual(self.library.album_details('Conflicting name'), local)
        self.assertEqual(self.library.query()['total'], 0)
        self.assertEqual(self.library.setting('server_version', 0), 0)

    def test_native_frame_rejects_truncation_trailing_data_and_duplicate_fields(self):
        response = self.page(self.records[0], True)
        value = dict(since=0, cursor=None, limit=200, response=response,
                     decoded=[self.client.open_record(self.records[0])])
        data = json.dumps(value).encode()
        frame = struct.pack('!I', len(data))+data
        duplicate = data[:-1]+b',"si\\u006ece":0}'
        for incoming in [frame[:3], frame[:-1], frame+b'extra',
                         struct.pack('!I', 128*1024*1024+1),
                         struct.pack('!I', len(duplicate))+duplicate]:
            result = subprocess.run([str(sync_pages.program()), str(self.library.root/'library.sqlite')],
                                    input=incoming, capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(result.stderr)
            self.assertEqual(self.library.query()['total'], 0)
            self.assertEqual(self.library.setting('server_version', 0), 0)

    def test_helper_loss_and_caller_transaction_have_no_python_write_fallback(self):
        response = self.page(self.records[0], True)
        decoded = [self.client.open_record(self.records[0])]
        with patch.object(sync_pages.subprocess, 'run', side_effect=OSError('unavailable')):
            with self.assertRaisesRegex(ValueError, 'preserved'):
                sync_pages.apply(self.library, response, 0, None, decoded, 200)
        self.library.db.execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaisesRegex(ValueError, 'Finish the current library edit'):
                sync_pages.apply(self.library, response, 0, None, decoded, 200)
        finally:
            self.library.db.rollback()
        self.assertEqual(self.library.query()['total'], 0)
        self.assertEqual(self.library.setting('server_version', 0), 0)


if __name__ == '__main__':
    unittest.main()
