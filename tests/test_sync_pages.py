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
from photos.sync import Client, PHOTOS, ALBUMS, ANNOTATIONS
from owner_fixture import ACCOUNT
from photos import sync_pages, semantic, people


class SyncPagesTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.library = Library(root / 'library')
        self.addCleanup(self.library.db.close)
        self.client = Client(self.library, dict(node='http://127.0.0.1:18080', node_id='a' * 64), account=ACCOUNT)
        source = root / 'Camera'
        source.mkdir()
        self.records = []
        self.annotations = []
        for index, color in enumerate(['red', 'blue']):
            path = source / f'{index}.jpg'
            Image.new('RGB', (100, 80), color).save(path)
        self.library.import_folders([source])
        self.ids = [digest_file(path) for path in sorted(source.iterdir())]
        self.sizes = [path.stat().st_size for path in sorted(source.iterdir())]
        for photo_id in self.ids:
            record, annotation = self.client.record(self.library.photo(photo_id), dict(blobs=[]))
            self.records.append(record)
            self.annotations.append(annotation)
        self.library.db.execute('DELETE FROM photos')
        self.library.db.commit()

    def page(self, record, complete, version=10, cursor_version=5):
        result = dict(status='ok', encrypted_records_only=True, server_version=version,
                      changes_complete=complete, changes=dict(encrypted_records=[record]))
        if not complete:
            result['record_cursor'] = dict(version=cursor_version, collection=record['collection'], id=record['id'])
        return result

    def album(self, name, photos=None, entity='d' * 32, updated=2000000000, **fields):
        value = dict(name=name, description='', cover='', pinned=False, archived=False,
                     deleted=False, created_at=1, updated_at=int(updated), **fields)
        if photos is not None:
            value['photos'] = photos  # As albums were written before photos named them.
        return self.client.seal_entity(value, 'albums', entity, 'entity.album.v1', updated)

    def photo(self, index, albums, updated=2000000000):
        value = dict(hash=self.ids[index], size=self.sizes[index], type='image/jpeg', taken_at=1792800000,
                     width=100, height=80, blobs=[], added_at=1792800000, albums=albums)
        return self.client.seal_entity(value, 'photos', self.ids[index], 'entity.photo.v1', updated)

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
                     {**initial, 'record_cursor': dict(version=6, collection=PHOTOS, id='other')}]
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

    def test_other_kinds_of_entity_data_and_tombstones_change_nothing(self):
        levels = dict(collection='private.entity.v1.levels', id='level-collection', key_id='entity-v1',
                      nonce='n', ciphertext='c', updated_at='2026-10-23T00:00:00Z')
        tombstones = [dict(collection=PHOTOS, id=self.ids[0], updated_at='2026-10-23T00:00:00Z', deleted_at=1792800000),
                      dict(collection=ALBUMS, id='d' * 32, updated_at='2026-10-23T00:00:00Z', deleted_at='2026-10-23T00:00:00Z')]
        result = dict(status='ok', encrypted_records_only=True, server_version=7, changes_complete=True,
                      changes=dict(encrypted_records=[levels, *tombstones, self.records[0]]))
        self.client.request = Mock(return_value=result)
        self.client.exchange()
        self.assertEqual(self.library.query()['total'], 1)
        self.assertEqual(self.library.setting('server_version'), 7)
        # Only Photos' own collections may arrive without decoded data as tombstones.
        for record in [dict(collection='private.inbe.v1.notes', id='n', updated_at='2026-10-23T00:00:00Z'),
                       {**levels, 'collection': 'private.entity.v1.'}]:
            with self.assertRaises(ValueError):
                sync_pages.apply(self.library, {**result, 'server_version': 8, 'changes': dict(encrypted_records=[record])},
                                 7, None, [None], 200)
        self.assertEqual(self.library.setting('server_version'), 7)

    def test_album_metadata_encryption_is_bound_and_page_failure_preserves_local_edit(self):
        self.library.edit_album('', 'create', 'An empty album')
        self.library.edit_album('An empty album', 'description', 'Private description')
        record = self.client.album_record(self.library.album_details('An empty album'))
        self.assertEqual(record['collection'], ALBUMS)
        self.assertRegex(record['id'], '^[0-9a-f]{32}$')
        self.assertNotIn('Private description', json.dumps(record))
        self.library.edit_album('An empty album', 'pinned')
        self.client.accept(record)
        self.assertEqual(self.library.album_details('An empty album')['pinned'], 1)
        for changed in [{'collection': PHOTOS}, {'id': 'f' * 32}, {'nonce': 'tampered'}]:
            with self.subTest(changed=changed):
                response = self.page(self.records[0], True)
                response['changes']['encrypted_records'].append({**record, **changed})
                self.client.request = Mock(return_value=response)
                with self.assertRaises(ValueError):
                    self.client.exchange()
                self.assertEqual(self.library.query()['total'], 0)
                self.assertEqual(self.library.album_details('An empty album')['pinned'], 1)
                self.assertEqual(self.library.setting('server_version', 0), 0)

    def test_photo_records_name_their_albums_of_any_size(self):
        self.client.accept(self.album('Shared trip'))
        album = self.library.album_details('Shared trip')
        row = self.library.db.execute('SELECT * FROM albums WHERE id=?', (album['id'],)).fetchone()
        self.assertEqual(row['entity'], 'd' * 32)
        self.client.accept(self.photo(0, ['d' * 32, 'f' * 32]))
        self.assertEqual(json.loads(self.library.photo(self.ids[0])['albums']), ['Shared trip'])
        # An album this device does not have stays named when the photo is shared again.
        photo = self.library.photo(self.ids[0])
        value = self.client.photo_value(photo, json.loads(photo['manifest'])['entity'])
        self.assertEqual(value['albums'], ['d' * 32, 'f' * 32])
        # Album records never list photos, so an album of any size fits.
        self.assertNotIn('photos', self.client.album_value(row))
        # A photo that arrives before its album joins it when the album comes.
        self.client.accept(self.photo(1, ['e' * 32]))
        self.assertEqual(json.loads(self.library.photo(self.ids[1])['albums']), [])
        self.client.accept(self.album('Later', entity='e' * 32))
        self.assertEqual(json.loads(self.library.photo(self.ids[1])['albums']), ['Later'])
        # A newer album record renames it; a newer photo record leaves it.
        self.client.accept(self.album('Renamed trip', updated=2000000100))
        self.assertEqual(json.loads(self.library.photo(self.ids[0])['albums']), ['Renamed trip'])
        with self.assertRaises(ValueError):
            self.library.album_details('Shared trip')
        self.client.accept(self.photo(0, ['f' * 32], updated=2000000200))
        self.assertEqual(json.loads(self.library.photo(self.ids[0])['albums']), [])
        # An older album record changes nothing.
        self.client.accept(self.album('Old name', updated=2000000050))
        self.assertEqual(self.library.album_details('Renamed trip')['id'], album['id'])

    def test_unshared_album_of_the_same_name_adopts_the_record_and_keeps_its_photos(self):
        self.client.accept(self.records[0]); self.client.accept(self.records[1])
        self.library.edit(self.ids[0], 'album_add', 'Trip')
        local = self.library.album_details('Trip')
        # The local album is newer, so it keeps its details and is shared again.
        self.client.accept(self.album('Trip', updated=1))
        row = self.library.db.execute('SELECT * FROM albums WHERE id=?', (local['id'],)).fetchone()
        self.assertEqual((row['entity'], row['dirty']), ('d' * 32, 1))
        self.client.accept(self.photo(1, ['d' * 32], updated=2000000100))
        self.assertEqual(json.loads(self.library.photo(self.ids[1])['albums']), ['Trip'])
        # A second shared album of that name is shown apart.
        self.client.accept(self.album('Trip', entity='e' * 32))
        self.assertEqual(self.library.db.execute("SELECT entity FROM albums WHERE name='Trip (2)'").fetchone()[0], 'e' * 32)
        self.client.accept(self.photo(1, ['d' * 32, 'e' * 32], updated=2000000200))
        self.assertEqual(json.loads(self.library.photo(self.ids[1])['albums']), ['Trip', 'Trip (2)'])

    def test_album_still_listing_its_photos_shares_them_again_naming_it(self):
        self.client.accept(self.records[0]); self.client.accept(self.records[1])
        self.library.db.execute('UPDATE photos SET dirty=0'); self.library.db.commit()
        self.client.accept(self.album('Written before', [self.ids[1]]))
        photo = self.library.photo(self.ids[1])
        self.assertEqual((json.loads(photo['albums']), photo['dirty']), (['Camera', 'Written before'], 1))
        value = self.client.photo_value(photo, json.loads(photo['manifest'])['entity'])
        self.assertIn('d' * 32, value['albums'])
        self.assertEqual(self.library.photo(self.ids[0])['dirty'], 0)

    def test_record_from_another_app_keeps_fields_it_does_not_know(self):
        self.client.accept(self.records[0])
        self.library.edit(self.ids[0], 'caption', 'Said in Photos')
        self.library.edit(self.ids[0], 'favorite')
        photo = self.library.photo(self.ids[0])
        manifest = json.loads(photo['manifest']); manifest['entity']['confirmed'] = True
        self.library.db.execute('UPDATE photos SET manifest=?,dirty=0 WHERE id=?', (json.dumps(manifest), self.ids[0]))
        self.library.db.commit()
        diary = dict(hash=self.ids[0], size=photo['size'], type='image/jpeg', taken_at=1792800000, width=100,
                     height=80, blobs=[], added_at=1792800500, name='0.jpg', day='2026-10-23', caption='Said in Diary')
        self.client.accept(self.client.seal_entity(diary, 'photos', self.ids[0], 'entity.photo.v1', photo['updated'] + 10))
        photo = self.library.photo(self.ids[0])
        self.assertEqual((photo['caption'], photo['favorite'], photo['taken']), ('Said in Diary', 1, 1792800000))
        value = self.client.photo_value(photo, json.loads(photo['manifest'])['entity'])
        self.assertEqual((value['day'], value['favorite'], value['caption']), ('2026-10-23', True, 'Said in Diary'))

    def test_annotations_never_replace_queued_or_newer_local_data(self):
        self.client.accept(self.records[0])
        annotation = dict(id=self.ids[0], metadata=dict(camera='Fixture'), edits=dict(removed_albums=['Old']),
                          semantic=None, people=None)
        sealed = self.client.seal_annotation(annotation, 2000000000)
        self.client.accept(sealed)
        photo = self.library.photo(self.ids[0])
        self.assertEqual(json.loads(photo['metadata']), dict(camera='Fixture'))
        self.assertEqual(json.loads(photo['edits'])['removed_albums'], ['Old'])
        older = self.client.seal_annotation({**annotation, 'metadata': dict(camera='Older')}, 1999999999)
        self.client.accept(older)
        self.assertEqual(json.loads(self.library.photo(self.ids[0])['metadata']), dict(camera='Fixture'))
        self.library.edit(self.ids[0], 'caption', 'Queued edit')
        self.client.accept(self.client.seal_annotation({**annotation, 'metadata': dict(camera='Newer')}, 2000000001))
        self.assertEqual(json.loads(self.library.photo(self.ids[0])['metadata']), dict(camera='Fixture'))
        for edits in [dict(crop=[0, 0, 1, 1]), dict(removed_albums='Old'), dict(live_separated='yes')]:
            with self.assertRaises(ValueError):
                self.client.accept(self.client.seal_annotation({**annotation, 'edits': edits}, 2000000002))

    def test_native_write_failure_rolls_back_photos_albums_indexes_and_checkpoint(self):
        photo = self.client.open_record(self.records[0])
        annotation = self.client.open_record(self.annotations[0])
        vector = base64.b64encode(struct.pack('<512f', 1., *([0.]*511))).decode()
        annotation['semantic'] = dict(model=semantic.MODEL, vector=vector)
        annotation['people'] = dict(model=people.MODEL, faces=[], scanned=False, tags=[
            dict(id='e'*64, name='Private fixture', ignored=0, updated=1, birth='')])
        self.library.edit_album('', 'create', 'A remote album')
        album = self.client.album_record(self.library.album_details('A remote album'))
        self.library.db.execute('DELETE FROM albums'); self.library.db.commit()
        bad = self.client.open_record(self.records[1]); bad['size'] = 'bad'
        response = self.page(self.records[0], True)
        response['changes']['encrypted_records'] = [self.records[0], self.annotations[0], album, self.records[1]]
        with self.assertRaisesRegex(ValueError, 'Invalid photo metadata'):
            sync_pages.apply(self.library, response, 0, None,
                             [photo, annotation, self.client.open_record(album), bad], 200)
        for table in ['photos', 'albums', 'semantic_embeddings', 'people', 'person_tags']:
            self.assertEqual(self.library.db.execute('SELECT count(*) FROM '+table).fetchone()[0], 0)
        self.assertEqual(self.library.setting('server_version', 0), 0)
        self.assertIsNone(self.library.setting('record_cursor'))

    def test_native_merge_keeps_newer_local_edits_and_exact_unknown_metadata(self):
        metadata = dict(camera={'integer': 10**100, 'values': [None, True, '日本語\n\0']})
        annotation = self.client.seal_annotation(dict(id=self.ids[0], metadata=metadata), 2000000000)
        self.client.request = Mock(return_value=dict(status='ok', encrypted_records_only=True, server_version=10,
                                                     changes_complete=True, changes=dict(encrypted_records=[self.records[0], annotation])))
        self.client.exchange()
        self.assertEqual(json.loads(self.library.photo(self.ids[0])['metadata']), metadata)
        self.library.edit(self.ids[0], 'caption', 'Newer local edit')
        before = self.library.photo(self.ids[0])
        self.client.request = Mock(return_value=self.page(self.records[0], True, version=11))
        self.client.exchange()
        self.assertEqual(self.library.photo(self.ids[0]), before)
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
        self.client.album_record(self.library.album_details('Conflicting name'))
        local = self.library.album_details('Conflicting name')
        record = self.album('Conflicting name', [], entity='c' * 32, updated=local['updated'] + 100)
        # The client shows a second album of one name apart; a page that
        # still names both the same never replaces the local album.
        other = {**self.client.open_record(record), 'id': 'c' * 64, 'name': 'Conflicting name'}
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
