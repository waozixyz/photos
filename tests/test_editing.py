import json
from pathlib import Path
import tempfile
import unittest
from PIL import Image, ImageCms

from photos import editing
from photos.library import Library, LibraryError, digest_file, packed
from photos.worker import process


class EditingTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source = self.root / 'source/Fixture/photo.png'
        self.source.parent.mkdir(parents=True)
        image = Image.new('RGBA', (6, 4), (0, 0, 0, 0))
        image.putpixel((0, 0), (200, 30, 50, 128))
        image.putpixel((5, 3), (10, 100, 70, 255))
        image.save(self.source)
        self.source.with_suffix('.png.json').write_text(packed(dict(description='Original caption', tags='art',
            photoTakenTime=dict(timestamp='1696766400'), geoData=dict(latitude=-25.3, longitude=-57.6))))
        self.library = Library(self.root / 'library')
        self.library.import_folders([self.source.parent.parent])
        self.photo_id = digest_file(self.source)
        self.config = dict(root=str(self.library.root))

    def tearDown(self):
        self.library.db.close()
        self.directory.cleanup()

    def test_rotate_flip_copy_retains_metadata_alpha_and_originals(self):
        self.library.edit(self.photo_id, 'favorite')
        self.library.edit(self.photo_id, 'hidden')
        self.library.edit(self.photo_id, 'archived')
        parent = self.library.photo(self.photo_id)
        before = self.source.read_bytes()
        new_id = editing.save(self.library, self.photo_id, [90, 1, 0, 100, 100, 100, 0])
        copy = self.library.photo(new_id)
        self.assertEqual(self.library.photo(self.photo_id), parent)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(self.library.original(parent).read_bytes(), before)
        for field in ['taken', 'albums', 'caption', 'tags', 'favorite', 'hidden', 'archived']:
            self.assertEqual(copy[field], parent[field])
        self.assertEqual((copy['width'], copy['height']), (4, 6))
        self.assertEqual(json.loads(copy['metadata'])['photosEdit']['parent'], self.photo_id)
        self.assertEqual(self.library.details(new_id)['location'], '-25.3, -57.6')
        with Image.open(self.library.original(copy)) as image:
            self.assertEqual(image.mode, 'RGBA')
            self.assertEqual(image.getpixel((0, 0)), (200, 30, 50, 128))
        self.library.edit(new_id, 'caption', 'Later edit')
        self.library.edit(new_id, 'trash')
        self.assertEqual(editing.save(self.library, self.photo_id, [90, 1, 0, 100, 100, 100, 0]), new_id)
        self.assertEqual(self.library.photo(new_id)['caption'], 'Later edit')
        self.assertTrue(self.library.photo(new_id)['trashed'])

    def test_crop_and_adjustments_export_reimport_as_distinct_original(self):
        new_id = editing.save(self.library, self.photo_id, [0, 0, 0, 150, 110, 0, 1])
        copy = self.library.photo(new_id)
        self.assertEqual((copy['width'], copy['height']), (4, 4))
        self.assertEqual(digest_file(self.library.original(copy)), new_id)
        exported = Path(self.library.export(new_id, self.root / 'export'))
        self.assertEqual(exported.read_bytes(), self.library.original(copy).read_bytes())
        other = Library(self.root / 'restored')
        try:
            other.import_folders([exported.parent])
            self.assertEqual(other.photo(new_id)['caption'], copy['caption'])
            self.assertEqual(other.details(new_id)['location'], self.library.details(new_id)['location'])
            self.assertEqual(json.loads(other.photo(new_id)['metadata'])['photosEdit'], json.loads(copy['metadata'])['photosEdit'])
        finally:
            other.db.close()

    def test_preview_rpc_is_bounded_cancellable_and_invalid_edits_keep_library(self):
        token = ''
        for brightness in range(100, 151, 10):
            result = process(dict(action='image_preview', id=self.photo_id, value=packed([0, 0, 0, brightness, 100, 100, 0]), edit_session=token), self.config)
            token = result['edit_session']
            self.assertTrue(Path(result['edit_preview']).is_file())
            self.assertLessEqual(len(list(Path(result['edit_preview']).parent.glob('*.png'))), 2)
        self.assertEqual(self.library.query()['total'], 1)
        process(dict(action='image_close', edit_session=token), self.config)
        self.assertFalse((self.library.root / 'private/edit-previews' / token).exists())
        for value in ['not json', '[1,2]', '[0,0,0,NaN,100,100,0]', '[0,0,0,201,100,100,0]', '[0,0,0,100,100,100,0]']:
            with self.assertRaises(LibraryError):
                editing.save(self.library, self.photo_id, value)
        with self.assertRaises(LibraryError):
            editing.preview(self.library, self.photo_id, editing.DEFAULT, '../originals')
        self.library.edit(self.photo_id, 'trash')
        with self.assertRaises(LibraryError):
            editing.save(self.library, self.photo_id, [90, 0, 0, 100, 100, 100, 0])
        self.assertEqual(self.library.query()['total'], 1)

    def test_animated_image_is_refused_without_losing_frames(self):
        path = self.source.with_name('motion.gif')
        first = Image.new('RGB', (5, 5), 'red')
        first.save(path, save_all=True, append_images=[Image.new('RGB', (5, 5), 'blue')], duration=100, loop=0)
        self.library.import_folders([self.source.parent.parent])
        with self.assertRaisesRegex(LibraryError, 'Animated'):
            editing.save(self.library, digest_file(path), [90, 0, 0, 100, 100, 100, 0])
        with Image.open(path) as image:
            self.assertEqual(image.n_frames, 2)

    def test_native_processor_orients_exif_preserves_profile_and_bounds_preview(self):
        path = self.source.with_name('camera.jpg')
        image = Image.new('RGB', (2400, 1800), (80, 120, 160))
        exif = Image.Exif(); exif[274] = 6
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        image.save(path, exif=exif, icc_profile=profile)
        self.library.import_folders([self.source.parent.parent])
        photo_id = digest_file(path)
        data, dimensions = editing.render(self.library, photo_id, editing.DEFAULT, preview=True)
        self.assertEqual(dimensions, (960, 1280))
        from io import BytesIO
        with Image.open(BytesIO(data)) as preview:
            self.assertEqual(preview.info.get('icc_profile'), profile)
        new_id = editing.save(self.library, photo_id, [90, 0, 0, 100, 100, 100, 0])
        with Image.open(self.library.original(self.library.photo(new_id))) as copy:
            self.assertEqual(copy.size, (2400, 1800))
            self.assertEqual(copy.info.get('icc_profile'), profile)
            self.assertNotIn(274, copy.getexif())

    def test_location_edits_and_removal_survive_sidecar_changes_and_export(self):
        self.library.edit_many([self.photo_id], 'location', '90, -180')
        self.source.with_suffix('.png.json').write_text(packed(dict(geoData=dict(latitude=1, longitude=2), geoDataExif=dict(latitude=3, longitude=4))))
        self.library.import_folders([self.source.parent.parent])
        self.assertEqual(self.library.details(self.photo_id)['location'], '90.0, -180.0')
        for value in ['nan, 0', 'inf, 0', '91, 0', '0, -181', '0', '0, 1, 2']:
            with self.assertRaises(LibraryError):
                self.library.edit(self.photo_id, 'location', value)
        exported = Path(self.library.export(self.photo_id, self.root / 'location-export'))
        metadata = json.loads((exported.parent / 'metadata' / (exported.name + '.json')).read_bytes())
        self.assertEqual(metadata['geoData'], dict(latitude=90, longitude=-180))
        self.library.edit(self.photo_id, 'location', '')
        self.source.with_suffix('.png.json').write_text(packed(dict(geoData=dict(latitude=11, longitude=22))))
        self.library.import_folders([self.source.parent.parent])
        self.assertEqual(self.library.details(self.photo_id)['location'], '')
        before = self.library.photo(self.photo_id)
        with self.assertRaises(LibraryError):
            self.library.edit_many([self.photo_id, 'missing'], 'location', '10, 20')
        self.assertEqual(self.library.photo(self.photo_id), before)

    def test_memories_use_capture_day_utc_and_exclude_hidden_trash_archive(self):
        for year in range(2018, 2027):
            path = self.source.with_name(f'{year}.png')
            Image.new('RGB', (year - 2010, 8), 'red').save(path)
        self.library.import_folders([self.source.parent.parent])
        ids = [row[0] for row in self.library.db.execute('SELECT id FROM photos WHERE id<>? ORDER BY name', (self.photo_id,))]
        for index, photo_id in enumerate(ids):
            self.library.edit(photo_id, 'taken', f'{2018 + index}-10-08T12:00:00+00:00')
        self.library.edit(self.photo_id, 'taken', '2025-10-09T00:30:00+02:00')
        memories = self.library.query('memories', memory_date='2026-10-08', limit=2)
        self.assertEqual(memories['count'], 9)
        self.assertEqual(memories['memory_date'], '2026-10-08')
        self.assertEqual(memories['photos'][0]['date'], '2025-10-08')
        pages = [p['id'] for start in range(0, 9, 2) for p in self.library.query('memories', memory_date='2026-10-08', offset=start, limit=2)['photos']]
        self.assertEqual(len(pages), len(set(pages)))
        self.library.edit(ids[0], 'hidden')
        self.library.edit(ids[1], 'trash')
        self.library.edit(ids[2], 'archived')
        self.assertEqual(self.library.query('memories', memory_date='2026-10-08')['count'], 6)
        self.library.edit_album('Fixture', 'archived')
        self.assertEqual(self.library.query('memories', memory_date='2026-10-08')['count'], 0)
        for day in ['2026-02-30', '2026-1-1', 'arbitrary']:
            with self.assertRaises(LibraryError):
                self.library.query('memories', memory_date=day)
        result = process(dict(action='memory_date', value='2026-10-08'), self.config)
        self.assertEqual(result['view'], 'memories')
        self.assertEqual(result['memory_date'], '2026-10-08')
