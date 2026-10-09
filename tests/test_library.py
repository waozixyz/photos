import json
from pathlib import Path
import tempfile
import unittest
from PIL import Image
from photos.library import Library, digest_file
from photos.sync import Client, encrypt, decrypt
from unittest.mock import Mock, patch
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

    def test_archive_keeps_search_albums_and_originals_and_batch_changes_atomic(self):
        first = self.export('desktop', 'Holiday', 'red')
        second = self.export('desktop', 'Family', 'blue')
        self.library.import_folders([first.parents[1]])
        ids = [digest_file(first), digest_file(second)]
        self.library.edit_many(ids, 'archived_set', '1')
        self.assertEqual(self.library.query()['count'], 0)
        self.assertEqual(self.library.query('archive')['count'], 2)
        self.assertEqual(self.library.query(search='same')['count'], 2)
        self.assertEqual(self.library.query(album='Holiday')['count'], 1)
        with self.assertRaises(ValueError):
            self.library.edit_many([ids[0], 'missing'], 'archived_set', '0')
        self.assertEqual(self.library.query('archive')['count'], 2)
        self.library.edit(ids[0], 'hidden_set', '1')
        self.assertEqual(self.library.query(search='same')['count'], 1)
        self.assertEqual(self.library.query('archive')['count'], 1)
        self.library.edit(ids[0], 'hidden_set', '0')
        self.library.edit(ids[1], 'trash')
        self.assertEqual(self.library.query('archive')['count'], 1)
        self.library.edit(ids[1], 'restore')
        self.library.import_folders([first.parents[1]])
        self.assertEqual(self.library.query('archive')['count'], 2)
        self.library.edit_many(ids, 'archived_set', '0')
        self.assertEqual(self.library.query()['count'], 2)
        for path, photo_id in zip([first, second], ids):
            self.assertEqual(self.library.original(self.library.photo(photo_id)).read_bytes(), path.read_bytes())

    def test_uncategorized_retains_photo_removed_from_last_album(self):
        path = self.export('desktop', 'Holiday')
        self.library.import_folders([path.parents[1]])
        photo_id = digest_file(path)
        self.assertEqual(self.library.query('uncategorized')['count'], 0)
        self.library.edit(photo_id, 'album_remove', 'Holiday')
        self.assertEqual(self.library.query('uncategorized')['count'], 1)
        self.assertEqual(self.library.query('trash')['count'], 0)
        self.library.edit(photo_id, 'archived')
        self.assertEqual(self.library.query('uncategorized')['count'], 1)
        self.library.edit(photo_id, 'album_add', 'New album')
        self.assertEqual(self.library.query('uncategorized')['count'], 0)

    def test_empty_album_details_pinning_cover_and_archive_preserve_originals(self):
        first = self.export('desktop', 'Holiday', 'red')
        second = self.export('desktop', 'Family', 'blue')
        self.library.import_folders([first.parents[1]])
        photo_id = digest_file(first)
        self.library.edit_album('', 'create', 'Empty')
        self.library.rename_album('Empty', 'Empty renamed')
        self.assertEqual([a['name'] for a in self.library.album_list('empty')], ['Empty renamed'])
        self.library.edit_album('Holiday', 'description', 'Our holiday')
        self.library.edit_album('Holiday', 'pinned')
        self.library.edit_album('Holiday', 'cover', photo_id)
        self.assertEqual(self.library.album_list()[0]['name'], 'Holiday')
        self.assertEqual(self.library.album_details('Holiday')['description'], 'Our holiday')
        self.assertTrue(self.library.album_list()[0]['thumbnail'])
        with self.assertRaises(ValueError):
            self.library.edit_album('Family', 'cover', photo_id)
        self.library.edit(photo_id, 'album_add', 'Family')
        self.library.edit_album('Holiday', 'archived')
        self.assertEqual(self.library.query()['count'], 1)
        self.assertEqual(self.library.query('archive')['count'], 1)
        self.assertEqual(self.library.query(album='Family')['count'], 2)
        self.assertEqual(self.library.query(search='same')['count'], 2)
        self.assertEqual([a['name'] for a in self.library.album_list('archived')], ['Holiday'])
        self.library.edit_album('Holiday', 'archived')
        self.assertEqual(self.library.query()['count'], 2)
        self.library.edit(photo_id, 'hidden')
        self.assertEqual(self.library.album_list()[0]['thumbnail'], '')
        with self.assertRaises(ValueError):
            self.library.edit_album('Holiday', 'delete_empty')
        self.library.edit_album('Empty renamed', 'delete_empty')
        self.assertNotIn('Empty renamed', [a['name'] for a in self.library.album_list('all')])
        self.assertEqual(self.library.original(self.library.photo(photo_id)).read_bytes(), first.read_bytes())

    def test_album_catalog_preserves_last_removed_membership_and_atomic_rename(self):
        path = self.export('desktop', 'Holiday')
        self.library.import_folders([path.parents[1]])
        photo_id = digest_file(path)
        self.library.edit(photo_id, 'album_remove', 'Holiday')
        self.assertEqual(self.library.album_list()[0]['count'], 0)
        self.library.rename_album('Holiday', 'Renamed empty')
        self.library.edit_album('', 'create', 'Taken')
        with self.assertRaises(ValueError):
            self.library.rename_album('Renamed empty', 'Taken')
        self.assertEqual(self.library.album_details('Renamed empty')['name'], 'Renamed empty')
        self.library.import_folders([path.parents[1]])
        self.assertEqual(self.library.query('uncategorized')['count'], 1)
        self.assertEqual(len(self.library.album_list('all')), 2)
        extra = path.parent / 'new.jpg'
        Image.new('RGB', (80, 100), 'green').save(extra)
        self.library.import_folders([path.parents[1]])
        self.assertEqual(self.library.query(album='Holiday')['count'], 1)
        self.assertNotEqual(self.library.album_details('Holiday')['id'], self.library.album_details('Renamed empty')['id'])

    def test_empty_original_restores_from_its_verified_empty_manifest(self):
        source = self.root / 'Camera'
        source.mkdir()
        path = source / 'empty.jpg'
        path.write_bytes(b'')
        self.library.import_folders([source])
        photo_id = digest_file(path)
        photo = self.library.photo(photo_id)
        client = Client(self.library, dict(node='http://127.0.0.1:18080', node_id='a' * 64))
        client.save_manifest(photo, client.prepare_original(photo))
        self.library.original(photo).unlink()
        client.fetch_blob = Mock(side_effect=AssertionError('An empty original needs no blob'))
        restored = Path(client.materialize(photo_id))
        self.assertEqual(restored.read_bytes(), b'')
        self.assertEqual(digest_file(restored), photo_id)
        restored.unlink()
        self.library.db.execute('UPDATE photos SET size=1 WHERE id=?', (photo_id,))
        with self.assertRaises(ValueError):
            client.materialize(photo_id)
        self.assertFalse(restored.exists())

    def test_sorting_pages_are_consistent_in_both_directions(self):
        from photos.worker import library_request
        first = self.export('desktop', 'Old', 'red')
        second = self.export('desktop', 'New', 'blue')
        self.library.import_folders([first.parents[1]])
        ids = [digest_file(first), digest_file(second)]
        self.library.db.execute('UPDATE photos SET taken=? WHERE id=?', (1700000000, ids[1]))
        self.library.db.commit()
        self.assertEqual([p['id'] for p in self.library.query()['photos']], ids[::-1])
        self.assertEqual([p['id'] for p in library_request(self.library, dict(sort='oldest'), {})['photos']], ids)
        for sort, order in [('oldest', ids), ('newest', ids[::-1])]:
            pages = [self.library.query(offset=i, limit=1, sort=sort)['photos'][0]['id'] for i in range(2)]
            self.assertEqual(pages, order)
        with self.assertRaises(ValueError):
            self.library.query(sort='unexpected')

    def test_legacy_store_and_legacy_encrypted_records_default_to_unarchived(self):
        import sqlite3
        path = self.export('desktop', 'Holiday')
        self.library.import_folders([path.parents[1]])
        photo_id = digest_file(path)
        legacy = self.root / 'legacy'
        legacy.mkdir()
        photo = self.library.photo(photo_id)
        photo.pop('archived')
        schema = self.library.db.execute("SELECT sql FROM sqlite_master WHERE name='photos'").fetchone()[0]
        schema = schema.replace('archived INTEGER NOT NULL DEFAULT 0,', '')
        with sqlite3.connect(legacy / 'library.sqlite') as db:
            db.execute(schema)
            db.execute('INSERT INTO photos(' + ','.join(photo) + ') VALUES(' + ','.join('?' for _ in photo) + ')', list(photo.values()))
        migrated = Library(legacy)
        self.addCleanup(migrated.db.close)
        self.assertEqual(migrated.photo(photo_id)['archived'], 0)
        self.assertEqual(migrated.query()['count'], 1)
        client = Client(migrated, dict(node='http://127.0.0.1:18080', node_id='a' * 64))
        record = client.record(photo, dict(chunks=[]))
        migrated.db.execute('DELETE FROM photos')
        client.accept(record)
        self.assertEqual(migrated.photo(photo_id)['archived'], 0)
        photo['archived'] = 2
        with self.assertRaises(ValueError):
            client.accept(client.record(photo, dict(chunks=[])))

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

    def test_batch_edits_are_atomic_explicit_and_keep_originals(self):
        first = self.export('desktop', 'Holiday', 'red')
        second = self.export('desktop', 'Family', 'blue')
        self.library.import_folders([first.parents[1]])
        ids = [digest_file(first), digest_file(second)]
        self.library.edit(ids[0], 'favorite')
        self.library.edit_many(ids + ids, 'favorite_set', '1')
        self.assertEqual(self.library.query('favorites')['count'], 2)
        with self.assertRaises(ValueError):
            self.library.edit_many([ids[0], 'missing'], 'hidden_set', '1')
        self.assertEqual(self.library.query()['count'], 2)
        self.library.edit_many(ids, 'trash')
        self.assertEqual(self.library.query('trash')['count'], 2)
        self.library.edit_many(ids, 'restore')
        self.library.edit_many(ids, 'album_add', 'Together')
        self.assertEqual(self.library.query(album='Together')['count'], 2)
        for path, photo_id in zip([first, second], ids):
            self.assertEqual(self.library.original(self.library.photo(photo_id)).read_bytes(), path.read_bytes())

    def test_date_and_album_edits_survive_changed_import_metadata(self):
        path = self.export('desktop', 'Holiday')
        self.library.import_folders([path.parents[1]])
        photo_id = digest_file(path)
        self.library.edit(photo_id, 'taken', '2024-06-15 14:30:00+02:00')
        corrected = self.library.photo(photo_id)['taken']
        self.library.edit(photo_id, 'album_remove', 'Holiday')
        sidecar = path.parent / 'metadata/same.jpg.json'
        metadata = json.loads(sidecar.read_text())
        metadata['description'] = 'Changed externally'
        metadata['photoTakenTime']['timestamp'] = '1500000000'
        sidecar.write_text(json.dumps(metadata))
        self.library.import_folders([path.parents[1]])
        self.assertEqual(self.library.photo(photo_id)['taken'], corrected)
        self.assertEqual(self.library.query(album='Holiday')['count'], 0)
        self.assertEqual(self.library.query()['count'], 1)
        self.assertEqual(self.library.details(photo_id)['taken'], '2024-06-15T12:30:00+00:00')
        with self.assertRaises(ValueError):
            self.library.edit(photo_id, 'taken', '2024-99-99')
        self.assertEqual(self.library.photo(photo_id)['taken'], corrected)

    def test_album_rename_keeps_other_memberships_hidden_items_and_rescan(self):
        first = self.export('desktop', 'Holiday', 'red')
        second = self.export('desktop', 'Holiday 2', 'blue')
        self.library.import_folders([first.parents[1]])
        ids = [digest_file(first), digest_file(second)]
        self.library.edit_many(ids, 'album_add', 'Together')
        self.library.edit(ids[1], 'hidden')
        self.library.rename_album('Together', 'Trip')
        for photo_id in ids:
            self.assertIn('Trip', json.loads(self.library.photo(photo_id)['albums']))
            self.assertNotIn('Together', json.loads(self.library.photo(photo_id)['albums']))
        with self.assertRaises(ValueError):
            self.library.rename_album('Trip', 'Holiday')
        self.assertEqual(self.library.query(album='Trip')['count'], 1)
        self.library.rename_album('Holiday', 'Summer')
        sidecar = first.parent / 'metadata/same.jpg.json'
        sidecar.write_text(sidecar.read_text() + ' ')
        self.library.import_folders([first.parents[1]])
        self.assertEqual(self.library.query(album='Holiday')['count'], 0)
        self.assertEqual(self.library.query(album='Summer')['count'], 1)

    def test_selected_details_and_batch_requests_reach_the_same_library(self):
        from photos.worker import library_request
        first = self.export('desktop', 'Holiday', 'red')
        second = self.export('desktop', 'Family', 'blue')
        self.library.import_folders([first.parents[1]])
        ids = [digest_file(first), digest_file(second)]
        result = library_request(self.library, dict(action='favorite_set', ids=ids, value='1'), {})
        self.assertEqual(sum(p['favorite'] for p in result['photos']), 2)
        result = library_request(self.library, dict(action='open', id=ids[0]), {})
        self.assertEqual(result['detail']['caption'], 'A preserved caption')
        self.assertEqual(result['detail']['albums'], 'Holiday')
        self.assertNotIn('metadata', result['detail'])
        self.assertNotIn('detail', library_request(self.library, {}, {}))
        library_request(self.library, dict(action='rename', id=ids[0], value='New title.jpg'), {})
        self.assertEqual(self.library.photo(ids[0])['name'], 'New title.jpg')
        self.assertEqual(digest_file(self.library.original(self.library.photo(ids[0]))), ids[0])
        for invalid in ['..', ' .. ', '../other.jpg', 'line\nbreak.jpg']:
            with self.assertRaises(ValueError):
                self.library.edit(ids[0], 'rename', invalid)

    def test_edit_markers_are_encrypted_and_synced(self):
        path = self.export('desktop', 'Holiday')
        self.library.import_folders([path.parents[1]])
        photo_id = digest_file(path)
        self.library.edit(photo_id, 'taken', '2024-06-15')
        self.library.edit(photo_id, 'album_remove', 'Holiday')
        self.library.edit(photo_id, 'archived_set', '1')
        client = Client(self.library, dict(node='http://127.0.0.1:18080', node_id='a' * 64))
        record = client.record(self.library.photo(photo_id), dict(chunks=[]))
        self.assertNotIn('removed_albums', json.dumps(record))
        self.library.db.execute('DELETE FROM photos WHERE id=?', (photo_id,))
        client.accept(record)
        photo = self.library.photo(photo_id)
        self.assertEqual(json.loads(photo['edits']), dict(taken=True, removed_albums=['Holiday']))
        self.assertEqual(photo['dirty'], 0)
        self.assertEqual(photo['archived'], 1)

    def test_edits_during_import_metadata_read_are_preserved(self):
        path = self.export('desktop', 'Holiday')
        self.library.import_folders([path.parents[1]])
        photo_id = digest_file(path)
        sidecar = path.parent / 'metadata/same.jpg.json'
        sidecar.write_text(sidecar.read_text() + ' ')
        other = Library(self.library.root)
        self.addCleanup(other.db.close)
        opened = Image.open
        def during_read(*args, **kwargs):
            other.edit(photo_id, 'taken', '2024-06-15')
            other.edit(photo_id, 'album_remove', 'Holiday')
            return opened(*args, **kwargs)
        with patch('photos.library.Image.open', side_effect=during_read):
            self.library.import_folders([path.parents[1]])
        self.assertEqual(self.library.query(album='Holiday')['count'], 0)
        self.assertEqual(self.library.details(photo_id)['taken'], '2024-06-15T00:00:00+00:00')

    def test_original_copy_keeps_writer_available_and_merges_a_concurrent_import(self):
        from photos import importing
        first=self.export('desktop','Holiday')
        self.library.import_folders([first.parents[1]])
        second=self.export('desktop','New album','blue')
        old_id,new_id=digest_file(first),digest_file(second)
        other=Library(self.library.root);self.addCleanup(other.db.close)
        actual_copy=importing.copy;entered=False
        def during_copy(*args,**kwargs):
            nonlocal entered
            result=actual_copy(*args,**kwargs)
            if not entered:
                entered=True
                other.edit(old_id,'favorite_set','1')
                self.assertEqual(other.import_file(second,second.parents[1]),'added')
                other.edit(new_id,'taken','2024-06-15')
                other.edit(new_id,'album_remove','New album')
            return result
        with patch('photos.importing.copy',side_effect=during_copy):
            self.assertEqual(self.library.import_file(second,second.parents[1]),'merged')
        self.assertEqual(self.library.query()['total'],2)
        self.assertEqual(self.library.photo(old_id)['favorite'],1)
        self.assertEqual(self.library.details(new_id)['taken'],'2024-06-15T00:00:00+00:00')
        self.assertNotIn('New album',json.loads(self.library.photo(new_id)['albums']))
        self.assertEqual(self.library.original(self.library.photo(new_id)).read_bytes(),second.read_bytes())

    def test_exported_tags_favorites_and_album_metadata_round_trip(self):
        path = self.export('desktop', 'Holiday')
        self.library.import_folders([path.parents[1]])
        photo_id = digest_file(path)
        self.library.edit(photo_id, 'tags', 'forest hiking')
        self.library.edit(photo_id, 'favorite_set', '1')
        self.library.edit(photo_id, 'album_add', 'Family')
        self.library.edit(photo_id, 'archived_set', '1')
        self.library.export(photo_id, self.root / 'exported')
        other = Library(self.root / 'second')
        self.addCleanup(other.db.close)
        other.import_folders([self.root / 'exported'])
        self.assertEqual(other.query('favorites', search='hiking')['count'], 1)
        self.assertEqual(other.query(album='Family')['count'], 1)
        self.assertEqual(other.query(album='Holiday')['count'], 1)
        self.assertEqual(other.query('archive')['count'], 1)
        self.assertEqual(other.query()['count'], 0)

    def test_large_imported_caption_cannot_be_silently_truncated_by_editor(self):
        path = self.export('desktop', 'Holiday')
        sidecar = path.parent / 'metadata/same.jpg.json'
        metadata = json.loads(sidecar.read_text())
        metadata['description'] = 'é' * 10000
        sidecar.write_text(json.dumps(metadata))
        self.library.import_folders([path.parents[1]])
        photo_id = digest_file(path)
        details = self.library.details(photo_id)
        self.assertEqual(details['caption_editable'], 0)
        self.assertLessEqual(len(details['caption'].encode()), 8192)
        self.assertEqual(self.library.photo(photo_id)['caption'], metadata['description'])

    def test_adding_an_import_source_keeps_previously_added_sources(self):
        from photos.worker import library_request
        first = self.export('desktop', 'Holiday')
        second = self.export('phone', 'Camera')
        config = dict(sources=[])
        library_request(self.library, dict(action='import', value=str(first.parents[1])), config)
        library_request(self.library, dict(action='import', value=str(second.parents[1])), config)
        self.assertEqual(self.library.setting('sources'), [str(first.parents[1]), str(second.parents[1])])

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
