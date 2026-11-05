import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from photos.library import Library, LibraryError, digest_file
from photos.worker import process
from photos.sync import Client
from owner_fixture import ACCOUNT
from photos.export import Export


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.library = Library(self.root / 'library')
        self.addCleanup(self.library.db.close)
        source = self.root / 'source/Summer trip'
        source.mkdir(parents=True)
        self.ids = []
        for i, name in enumerate(['Beach 100%_fun.png', 'Birthday cake.png', 'Café.png', 'Private.png', 'Deleted.png']):
            path = source / name
            Image.new('RGB', (40 + i, 30), (100 + i, 70, 50)).save(path)
            self.ids.append(digest_file(path))
        self.library.import_folders([source.parent])
        for i, photo_id in enumerate(self.ids):
            self.library.edit(photo_id, 'taken', f'2024-06-{15+i:02d}T12:00:00Z')
        self.library.edit(self.ids[0], 'favorite_set', '1')
        self.library.edit(self.ids[0], 'tags', 'family, beach')
        self.library.edit(self.ids[0], 'location', '-25.3, -57.6')
        self.library.edit(self.ids[1], 'tags', 'work')
        self.library.edit(self.ids[1], 'caption', 'A birthday cake with family')
        self.library.edit(self.ids[2], 'archived_set', '1')
        self.library.edit(self.ids[3], 'hidden_set', '1')
        self.library.edit(self.ids[4], 'trash')
        self.config = dict(root=str(self.library.root))

    def results(self, search, **values):
        return [p['id'] for p in self.library.query(search=search, **values)['photos']]

    def test_combined_capture_dates_properties_albums_and_negative_filters(self):
        self.assertEqual(self.results('type:photo year:2024 album:"Summer trip" -tag:work'), self.ids[2::-2])
        self.assertEqual(self.results('is:favorite has:location from:2024-06-15 to:2024-06-15'), self.ids[:1])
        self.assertEqual(self.results('on:2024-06-16 caption:"birthday cake"'), self.ids[1:2])
        self.assertEqual(self.results('after:2024-06-15 before:2024-06-17'), self.ids[1:2])
        self.assertEqual(self.results('is:archived favorite:false has:tags'), [])
        self.assertEqual(self.results('is:archived'), self.ids[2:3])
        self.assertEqual(self.results('is:uncategorized'), [])
        self.assertEqual(self.results('type:video'), [])
        self.assertEqual(self.results('year:2024', limit=1, offset=1), self.ids[1:2])
        self.assertEqual(self.results('year:2024', sort='oldest'), self.ids[:3])

    def test_literals_quotes_unicode_and_injection_never_change_data(self):
        before = [dict(r) for r in self.library.db.execute('SELECT * FROM photos ORDER BY id')]
        self.assertEqual(self.results('name:"100%_fun"'), self.ids[:1])
        self.assertEqual(self.results('%'), self.ids[:1])
        self.assertEqual(self.results('name:"Café"'), self.ids[2:3])
        self.assertEqual(self.results("name:\"' OR 1=1 --\""), [])
        self.assertEqual(self.results("album:\"Summer trip' OR 1=1 --\""), [])
        self.assertEqual(self.results('caption:"birthday cake" -caption:family'), [])
        self.assertEqual(before, [dict(r) for r in self.library.db.execute('SELECT * FROM photos ORDER BY id')])

    def test_invalid_filters_and_dates_fail_without_mutating_library(self):
        for query in ['type:book', 'favorite:maybe', 'on:2024-02-30', 'year:0000', 'from:10000-01-01',
                      'album:"Unclosed', 'has:faces', 'tag:', 'x' * 513]:
            with self.subTest(query=query):
                with self.assertRaises(LibraryError): self.library.query(search=query)
        self.assertEqual(self.library.query()['total'], 5)

    def test_historic_dates_epoch_edges_leap_days_and_calendar_bounds(self):
        originals = {photo_id: self.library.original(self.library.photo(photo_id)).read_bytes() for photo_id in self.ids}
        dates = ['1900-02-28T12:00:00Z','1969-12-31T23:59:58.999Z','1970-01-01T00:00:00Z']
        for photo_id, date in zip(self.ids,dates): self.library.edit(photo_id,'taken',date)
        self.assertEqual(self.results('year:1900'),self.ids[:1])
        self.assertEqual(self.results('from:1900-02-28 to:1969-12-31'),self.ids[1::-1])
        self.assertEqual(self.results('on:1969-12-31'),self.ids[1:2])
        self.assertEqual(self.results('before:1970-01-01'),self.ids[1::-1])
        self.assertEqual(self.results('after:1969-12-31'),self.ids[2:3])
        self.assertEqual(self.results('on:1970-01-01'),self.ids[2:3])
        self.assertEqual(self.results('-year:1969'),[self.ids[2],self.ids[0]])
        for invalid in ['on:1900-02-29','on:0000-01-01','on:9999-02-29','year:10000','on:1969-13-01']:
            with self.assertRaises(LibraryError): self.results(invalid)
        self.library.edit(self.ids[0],'taken','0001-01-01T00:00:00Z')
        self.library.edit(self.ids[2],'taken','9999-12-31T23:59:59Z')
        self.assertEqual(self.results('year:0001'),self.ids[:1])
        self.assertEqual(self.results('year:9999'),self.ids[2:3])
        self.assertEqual(self.results('on:9999-12-31'),self.ids[2:3])
        self.assertEqual(self.results('to:0001-01-01'),self.ids[:1])
        self.assertEqual(self.library.query(search='year:0001')['photos'][0]['date'],'0001-01-01')
        self.library.edit(self.ids[1],'taken','1969-12-31T23:59:59Z')
        self.assertEqual(self.library.photo(self.ids[1])['taken'],-1)
        self.library.edit(self.ids[1],'taken','1970-01-01T00:00:00Z')
        self.assertEqual(self.library.photo(self.ids[1])['taken'],0)
        self.library.edit(self.ids[1],'taken','1969-12-31T23:59:59Z')
        for photo_id, data in originals.items(): self.assertEqual(self.library.original(self.library.photo(photo_id)).read_bytes(),data)

    def test_corrected_historic_date_survives_rescan_export_and_encrypted_restore(self):
        self.library.edit(self.ids[0],'taken','1900-02-28T23:30:00.125-02:00')
        taken=self.library.photo(self.ids[0])['taken']
        self.assertEqual(self.library.details(self.ids[0])['taken'],'1900-03-01T01:30:00.125000+00:00')
        self.library.import_folders([self.root/'source'])
        self.assertEqual(self.library.photo(self.ids[0])['taken'],taken)
        client=Client(self.library,dict(node='http://127.0.0.1:18080',node_id='a'*64),account=ACCOUNT)
        peer=Library(self.root/'peer');self.addCleanup(peer.db.close)
        receiver=Client(peer,dict(node='http://127.0.0.1:18080',node_id='a'*64),client.identity_path,account=ACCOUNT)
        receiver.accept(client.record(self.library.photo(self.ids[0]), dict(blobs=[])))
        peer.db.commit()
        self.assertEqual(peer.photo(self.ids[0])['taken'],taken)
        self.assertEqual(peer.query(search='on:1900-03-01')['photos'][0]['id'],self.ids[0])
        target=self.root/'export'
        with Export(self.library,str(target)) as exporter: exporter.run()
        restored=Library(self.root/'restored');self.addCleanup(restored.db.close)
        restored.import_folders([target])
        self.assertEqual(restored.photo(self.ids[0])['taken'],taken)
        self.assertEqual(restored.query(search='year:1900')['photos'][0]['id'],self.ids[0])

    def test_smart_album_updates_excludes_hidden_and_trash_and_retains_originals(self):
        request = dict(action='album_smart_create', value='Family favorites', search='is:favorite tag:family')
        result = process(request, self.config)
        self.assertEqual(result['album'], 'Family favorites')
        self.assertEqual(result['search'], '')
        self.assertEqual(result['count'], 1)
        self.assertEqual(self.library.album_details('Family favorites')['filter'], 'is:favorite tag:family')
        self.library.edit(self.ids[1], 'tags', 'family')
        self.library.edit(self.ids[1], 'favorite_set', '1')
        self.assertEqual(self.library.query(album='Family favorites')['count'], 2)
        album = next(a for a in self.library.album_list() if a['name'] == 'Family favorites')
        self.assertEqual(album['count'], 2); self.assertTrue(album['smart'])
        self.library.edit(self.ids[1], 'hidden_set', '1')
        self.assertEqual(self.library.query(album='Family favorites')['count'], 1)
        self.assertEqual(self.library.query('hidden', album='Family favorites', hidden_access=True)['count'], 0)
        self.assertEqual(self.library.query('trash', album='Family favorites', hidden_access=True)['count'], 0)
        with self.assertRaises(LibraryError): self.library.edit(self.ids[0], 'album_add', 'Family favorites')
        with self.assertRaises(LibraryError): self.library.edit(self.ids[0], 'album_remove', 'Family favorites')
        self.library.edit_album('Family favorites', 'filter', 'year:2024 -is:favorite')
        self.assertEqual(self.library.query(album='Family favorites')['count'], 1)
        self.library.rename_album('Family favorites', 'Smart renamed')
        self.library.edit_album('Smart renamed', 'delete_empty')
        self.assertEqual(self.library.query()['total'], 5)
        self.assertTrue(all(self.library.original(self.library.photo(photo_id)).is_file() for photo_id in self.ids))

    def test_smart_album_encrypted_records_and_export_definition_round_trip(self):
        self.library.edit_album('', 'smart_create', 'Beach', search='tag:beach year:2024')
        client = Client(self.library, dict(node='http://127.0.0.1:18080', node_id='a' * 64), account=ACCOUNT)
        record = client.album_record(self.library.album_details('Beach'))
        self.assertNotIn('tag:beach', json.dumps(record))
        peer = Library(self.root / 'peer'); self.addCleanup(peer.db.close)
        receiver = Client(peer, dict(node='http://127.0.0.1:18080', node_id='a' * 64), client.identity_path, account=ACCOUNT)
        receiver.accept_album(record)
        self.assertEqual(peer.album_details('Beach')['filter'], 'tag:beach year:2024')
        target = self.root / 'export'
        with Export(self.library, str(target)) as exporter: exporter.run()
        restored = Library(self.root / 'restored'); self.addCleanup(restored.db.close)
        restored.import_folders([target])
        self.assertEqual(restored.album_details('Beach')['filter'], 'tag:beach year:2024')
        self.assertEqual(restored.query(album='Beach')['count'], 1)
        self.assertEqual(restored.original(restored.photo(self.ids[0])).read_bytes(), self.library.original(self.library.photo(self.ids[0])).read_bytes())
