import json
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image, TiffImagePlugin

from photos.library import Library, LibraryError, digest_file
from photos.worker import process
from photos import query


class LocationsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.library = Library(self.root / 'library')
        self.addCleanup(self.library.db.close)
        self.ids = []
        source = self.root / 'source/Trip'
        source.mkdir(parents=True)
        for index in range(7):
            path = source / f'Photo {index}.png'
            Image.new('RGB', (40 + index, 30), (100 + index, 80, 60)).save(path)
            self.ids.append(digest_file(path))
        self.library.import_folders([source.parent])
        for photo_id, location in zip(self.ids, ['-25.3, -57.6', '-25.31, -57.61', '0, 0', '90, 180', '1, 2', '1, 2', '']):
            self.library.edit(photo_id, 'location', location)
        self.library.edit(self.ids[1], 'archived_set', '1')
        self.library.edit(self.ids[4], 'hidden_set', '1')
        self.library.edit(self.ids[5], 'trash')
        self.config = dict(root=str(self.library.root))

    def test_global_clusters_album_search_and_bounds_keep_zero_and_date_line(self):
        result = process(dict(view='map'), self.config)
        self.assertEqual(result['count'], 4)
        self.assertEqual(sum(marker['count'] for marker in result['markers']), 4)
        self.assertLessEqual(len(result['markers']), 96)
        self.assertTrue(Path(result['map_image']).is_file())
        self.assertIn((0, 0), [(m['latitude'], m['longitude']) for m in result['markers']])
        self.assertIn((90000000, 180000000), [(m['latitude'], m['longitude']) for m in result['markers']])
        cluster = next(m for m in result['markers'] if m['count'] == 2)
        result = process(dict(view='map', map_bounds=cluster['bounds'], map_gallery=True), self.config)
        self.assertEqual({p['id'] for p in result['photos']}, set(self.ids[:2]))
        self.assertEqual(result['markers'], [])
        self.assertEqual(self.library.query('map', album='Trip', search='name:"Photo 0"')['count'], 1)
        self.assertEqual(self.library.query('map', album="Trip' OR 1=1 --")['count'], 0)
        self.assertEqual(self.library.query('map', hidden_access=True)['count'], 4)
        self.library.edit(self.ids[0], 'location', '')
        self.assertEqual(self.library.query('map')['count'], 3)

    def test_invalid_bounds_rejected_without_changes_or_lock_leak(self):
        for bounds in [[], [1, 2, 3], [1, 2, 3, 4, 5], [10, 0, 0, 10], [-180000001, 0, 0, 0],
                       [0, -90000001, 1, 1], [0, 0, 1, 90000001], [0, 0, .5, 1], ['0', 0, 1, 1], {}]:
            with self.subTest(bounds=bounds):
                with self.assertRaises(LibraryError):
                    self.library.query('map', map_bounds=bounds)
                self.assertFalse(self.library.db.in_transaction)
        self.assertEqual(self.library.query()['total'], 7)
        self.assertEqual(self.library.query('map', map_bounds=[0, 0, 0, 0])['count'], 1)

    def test_large_library_clusters_include_unloaded_photos_with_bounded_response(self):
        template = dict(self.library.photo(self.ids[0]))
        columns = list(template)
        statement = 'INSERT INTO photos (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')'
        for index in range(1500):
            photo = {**template, 'id': hashlib.sha256(f'Synthetic map {index}'.encode()).hexdigest(),
                     'metadata': json.dumps({'geoData': {'latitude': -89 + index % 179,
                                                        'longitude': -179 + index % 359}})}
            self.library.db.execute(statement, [photo[column] for column in columns])
        self.library.db.commit()
        result = process(dict(view='map'), self.config)
        self.assertEqual(result['count'], 1504)
        self.assertEqual(result['photos'], [])
        self.assertEqual(len(self.library.query('map', map_gallery=True)['photos']), 60)
        self.assertEqual(sum(m['count'] for m in result['markers']), 1504)
        self.assertLessEqual(len(result['markers']), 96)
        self.assertLess(len(json.dumps(result).encode()), 150000)

    def test_invalid_coordinates_are_ignored_and_null_sidecar_falls_back_to_camera_gps(self):
        for latitude, longitude in [(91, 2), (1, 181), ('1', 2), (True, 2), (None, 2)]:
            metadata = json.dumps({'geoData': {'latitude': latitude, 'longitude': longitude}})
            self.library.db.execute('UPDATE photos SET metadata=? WHERE id=?', (metadata, self.ids[0]))
            self.library.db.commit()
            self.assertEqual(self.library.query('map', search='name:"Photo 0"')['count'], 0)
        metadata = json.dumps({'geoData': {'latitude': None, 'longitude': None},
                               'geoDataExif': {'latitude': 0, 'longitude': 0}})
        self.library.db.execute('UPDATE photos SET metadata=? WHERE id=?', (metadata, self.ids[0]))
        self.library.db.commit()
        result = self.library.query('map', search='name:"Photo 0"')
        self.assertEqual(result['count'], 1)
        self.assertEqual((result['markers'][0]['latitude'], result['markers'][0]['longitude']), (0, 0))

    def test_coordinates_cannot_race_a_concurrent_hide_during_cluster_publication(self):
        attempted, committed = threading.Event(), threading.Event()
        failures = []
        def hide():
            try:
                peer = Library(self.library.root)
                try:
                    attempted.set()
                    peer.edit(self.ids[0], 'hidden_set', '1')
                    committed.set()
                finally:
                    peer.db.close()
            except Exception as error:
                failures.append(error)
        original = query.page
        thread = None
        def paused(library, **request):
            nonlocal thread
            response = original(library, **request)
            thread = threading.Thread(target=hide)
            thread.start()
            self.assertTrue(attempted.wait(3))
            self.assertFalse(committed.wait(.15), 'Privacy edit overtook map publication')
            return response
        with patch.object(query, 'page', paused):
            result = self.library.query('map')
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(failures)
        self.assertTrue(committed.is_set())
        self.assertEqual(result['count'], 4)
        self.assertEqual(self.library.query('map')['count'], 3)

    def test_camera_gps_is_imported_without_changing_original_and_corrections_persist(self):
        path = self.root / 'camera/GPS.jpg'
        path.parent.mkdir()
        rational = TiffImagePlugin.IFDRational
        exif = Image.Exif()
        exif[34853] = {1: 'S', 2: (rational(25), rational(18), rational(0)),
                       3: 'W', 4: (rational(57), rational(36), rational(0))}
        Image.new('RGB', (30, 20), '#885522').save(path, exif=exif)
        original = path.read_bytes()
        self.library.import_folders([path.parent])
        photo_id = digest_file(path)
        self.assertEqual(self.library.details(photo_id)['location'], '-25.3, -57.6')
        self.assertEqual(self.library.query('map', album='camera')['count'], 1)
        self.library.edit(photo_id, 'location', '')
        path.with_name(path.name + '.json').write_text(json.dumps({'description': 'Rescan'}))
        self.library.import_folders([path.parent])
        self.assertEqual(self.library.query('map', album='camera')['count'], 0)
        self.assertEqual(self.library.original(self.library.photo(photo_id)).read_bytes(), original)
        self.assertEqual(path.read_bytes(), original)
