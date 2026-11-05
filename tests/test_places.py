import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from photos.library import Library, LibraryError, digest_file
from photos.worker import process
from photos import places
from photos.sync import Client
from owner_fixture import ACCOUNT
from photos.export import Export


class PlacesTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.library = Library(self.root / 'library')
        self.addCleanup(self.library.db.close)
        self.ids = []
        source = self.root / 'source/Trip'
        source.mkdir(parents=True)
        for index in range(12):
            path = source / f'Photo {index}.png'
            Image.new('RGB', (40 + index, 30), (90 + index, 70, 60)).save(path)
            self.ids.append(digest_file(path))
        self.library.import_folders([source.parent])
        coordinates = ['0, 0', '0, .01', '.008, .008', '0, 179.99', '0, -179.99',
                       '89.99, 180', '89.99, 0', '0, 0', '0, 0', '', '-25.3, -57.6', '0, 180']
        for i, (photo_id, value) in enumerate(zip(self.ids, coordinates)):
            self.library.edit(photo_id, 'location', value)
            self.library.edit(photo_id, 'taken', f'2024-06-{10+i:02d}T12:00:00Z')
        self.library.edit(self.ids[1], 'archived_set', '1')
        self.library.edit(self.ids[7], 'hidden_set', '1')
        self.library.edit(self.ids[8], 'trash')
        self.config = dict(root=str(self.library.root))

    def found(self, search='', **values):
        return {p['id'] for p in self.library.query(search=search, **values)['photos']}

    def create(self, name='Home', area='0, 0, 1.2', photo_id=''):
        return process(dict(action='place_create', value=json.dumps([name, area]), id=photo_id), self.config)

    def test_spherical_radius_edges_date_line_poles_and_missing_coordinates(self):
        self.assertEqual(self.found('near:0,0,1.2'), set(self.ids[:2]))
        # The diagonal is inside a 1.2-km bounding square but outside its circle.
        self.assertNotIn(self.ids[2], self.found('near:0,0,1.2'))
        self.assertEqual(self.found('near:0,180,2'), {self.ids[3], self.ids[4], self.ids[11]})
        self.assertEqual(self.found('near:90,0,1.2'), set(self.ids[5:7]))
        self.assertEqual(self.found('near:0,0,0.001'), {self.ids[0]})
        boundary = math.pi / 180 * 6371.0088 * .01
        self.assertIn(self.ids[1], self.found(f'near:0,0,{boundary:.15g}'))
        self.assertNotIn(self.ids[1], self.found(f'near:0,0,{boundary - .00001:.15g}'))
        self.assertEqual(len(self.found('near:0,0,20040')), 9)
        self.assertEqual(self.found('-near:0,0,20040'), {self.ids[9]})

    def test_named_plain_structured_negative_album_and_map_searches_remain_private(self):
        result = self.create()
        self.assertEqual(result['album'], 'Home')
        self.assertEqual(result['album_detail']['place'], 1)
        self.assertEqual(self.found('Home'), set(self.ids[:2]))
        self.assertEqual(self.found('place:home'), set(self.ids[:2]))
        self.assertEqual(self.found('place:Home year:2024 -name:"Photo 0"'), {self.ids[1]})
        self.assertEqual(self.found('place:Home', album='Trip'), set(self.ids[:2]))
        self.assertEqual(self.library.query('map', search='place:Home')['count'], 2)
        self.assertEqual(self.library.query('hidden', search='Home', hidden_access=False)['count'], 0)
        self.assertEqual(self.library.query('hidden', album='Home', hidden_access=True)['count'], 0)
        self.assertEqual(self.library.query('trash', album='Home', hidden_access=True)['count'], 0)
        places_view = process(dict(view='places', search='Home'), self.config)
        self.assertEqual([a['name'] for a in places_view['albums']], ['Home'])
        self.assertEqual(places_view['albums'][0]['place'], 1)
        self.create('Café 100%_area', '-25.3, -57.6, 2')
        self.assertEqual(self.found('place:"Café 100%_area"'), {self.ids[10]})
        self.assertEqual(self.found('name:"\' OR 1=1 --"'), set())
        self.assertEqual(self.found('place:"Home\' OR 1=1 --"'), set())

    def test_updates_renames_deletions_and_rescans_keep_originals_and_membership(self):
        before = {p: self.library.original(self.library.photo(p)).read_bytes() for p in self.ids}
        self.create(photo_id=self.ids[0])
        self.library.edit(self.ids[2], 'location', '0, .005')
        self.assertEqual(self.found('place:Home'), set(self.ids[:3]))
        process(dict(action='place_update', album='Home', value='0, 0, .001'), self.config)
        self.assertEqual(self.found('place:Home'), {self.ids[0]})
        self.library.rename_album('Home', 'Our home')
        self.assertFalse(self.found('place:Home'))
        self.assertEqual(self.found('place:"Our home"'), {self.ids[0]})
        self.library.edit_album('Our home', 'pinned')
        self.assertEqual(self.library.album_list('places')[0]['pinned'], 1)
        self.library.edit_album('Our home', 'delete_empty')
        self.assertFalse(self.found('place:"Our home"'))
        self.library.import_folders([self.root / 'source'])
        self.assertFalse(self.library.album_list('places'))
        for photo_id, original in before.items():
            self.assertEqual(self.library.original(self.library.photo(photo_id)).read_bytes(), original)
            self.assertEqual(json.loads(self.library.photo(photo_id)['albums']), ['Trip'])

    def test_bad_areas_coordinates_and_place_creation_fail_without_mutation(self):
        for area in ['', '0,0', '0,0,0', '0,0,-1', '0,0,20041', '91,0,1', '0,181,1', 'NaN,0,1',
                     '0,Infinity,1', '0,0,1,2', '0,0,1\0', ['0', 0, 1], True]:
            with self.subTest(area=area):
                with self.assertRaises(LibraryError): self.create(area=area)
        for value in ['near:0,0,NaN', 'near:0,0,1x', 'near:0,0,1,2', 'near:1e999,0,1', 'near:0,0,0.0001']:
            with self.subTest(search=value):
                with self.assertRaises(LibraryError): self.found(value)
        for photo_id in self.ids[7:9]:
            with self.assertRaises(LibraryError): places.create(self.library, json.dumps(['Home', '0,0,1']), photo_id)
        self.assertIn('error', self.create(photo_id=self.ids[7]))
        self.assertEqual(len(self.library.album_list('all')), 1)
        for latitude, longitude in [('0', 0), (True, 0), (None, 0), (91, 0)]:
            self.library.db.execute('UPDATE photos SET metadata=? WHERE id=?',
                                    (json.dumps({'geoData': dict(latitude=latitude, longitude=longitude)}), self.ids[0]))
            self.library.db.commit()
            self.assertFalse(self.found('near:0,0,0.001'))
            self.assertEqual(self.library.details(self.ids[0])['location'], '')
        self.library.db.execute('UPDATE photos SET metadata=? WHERE id=?', (json.dumps({
            'geoData': dict(latitude=None, longitude=None), 'geoDataExif': dict(latitude=0, longitude=0)}), self.ids[0]))
        self.library.db.commit()
        self.assertEqual(self.library.details(self.ids[0])['location'], '0, 0')
        self.assertFalse(self.library.db.in_transaction)

    def test_encrypted_export_import_update_and_tombstone_round_trip(self):
        self.create()
        sender = Client(self.library, dict(node='http://127.0.0.1:18080', node_id='a' * 64), account=ACCOUNT)
        peer = Library(self.root / 'peer'); self.addCleanup(peer.db.close)
        receiver = Client(peer, dict(node='http://127.0.0.1:18080', node_id='a' * 64), sender.identity_path, account=ACCOUNT)
        record = sender.album_record(self.library.album_details('Home'))
        self.assertNotIn('near:', json.dumps(record)); self.assertNotIn('Home', json.dumps(record))
        receiver.accept_album(record); peer.db.commit()
        self.assertEqual(peer.album_details('Home')['filter'], 'near:0,0,1.2')
        destination = self.root / 'export'
        with Export(self.library, str(destination)) as exporter: exporter.run()
        restored = Library(self.root / 'restored'); self.addCleanup(restored.db.close)
        restored.import_folders([destination])
        self.assertEqual({p['id'] for p in restored.query(search='place:Home')['photos']}, set(self.ids[:2]))
        places.update(self.library, 'Home', '90, 0, 2')
        receiver.accept_album(sender.album_record(self.library.album_details('Home'))); peer.db.commit()
        self.assertEqual(peer.album_details('Home')['filter'], 'near:90,0,2')
        self.library.edit_album('Home', 'delete_empty')
        tombstone = dict(self.library.db.execute('SELECT * FROM albums WHERE name=?', ('Home',)).fetchone())
        receiver.accept_album(sender.album_record(tombstone)); peer.db.commit()
        self.assertFalse(peer.album_list('places'))

    def test_concurrent_hide_before_place_publication_leaves_no_public_definition(self):
        from photos import query
        validate = query.validate
        def hide(library, search):
            peer = Library(library.root)
            try:
                peer.edit(self.ids[0], 'hidden_set', '1')
            finally:
                peer.db.close()
            return validate(library, search)
        with patch.object(query, 'validate', hide):
            with self.assertRaises(LibraryError):
                places.create(self.library, json.dumps(['Home', '0,0,1']), self.ids[0])
        self.assertFalse(self.library.album_list('places'))

    def test_radius_counts_all_matches_beyond_loaded_pages(self):
        import hashlib
        template = self.library.photo(self.ids[0])
        columns = list(template)
        statement = 'INSERT INTO photos (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')'
        for index in range(240):
            photo = {**template, 'id': hashlib.sha256(f'Synthetic place {index}'.encode()).hexdigest()}
            self.library.db.execute(statement, [photo[column] for column in columns])
        self.library.db.commit()
        self.create(area='0,0,.001')
        gallery = self.library.query(album='Home')
        self.assertEqual(gallery['count'], 241)
        self.assertEqual(len(gallery['photos']), 60)
        first = {p['id'] for p in gallery['photos']}
        last = self.library.query(album='Home', offset=240)
        self.assertEqual(len(last['photos']), 1)
        self.assertFalse(first & {p['id'] for p in last['photos']})
        self.assertEqual(self.library.album_list('places')[0]['count'], 241)
