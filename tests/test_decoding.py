from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from photos import decoding
from photos.library import Library, LibraryError, digest_file
from photos.worker import process as request
from codec_fixtures import dng, heic

def process(value, config):
    return request({**value, 'gallery_format': 2}, config)


class DecodeTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.source = self.root / 'source/Camera'
        self.source.mkdir(parents=True)
        self.library = Library(self.root / 'library')
        self.addCleanup(self.library.db.close)
        self.config = dict(root=str(self.library.root))

    def test_heic_pixels_alpha_rotation_and_raw_demosaic_preserve_originals(self):
        for name, create in [('Still.heic', heic), ('Rotated.heic', lambda p: heic(p, 6)),
                             ('Camera.dng', dng), ('Rotated.dng', lambda p: dng(p, 6))]:
            create(self.source / name)
        originals = {p: p.read_bytes() for p in self.source.iterdir()}
        result = self.library.import_folders([self.source.parent])
        self.assertEqual(result['added'], 4)
        for path, data in originals.items():
            photo_id = digest_file(path)
            photo = self.library.photo(photo_id)
            self.assertTrue(self.library.thumbnail(photo_id).is_file())
            self.assertFalse(decoding.display_path(self.library, photo).exists())
            reply = process(dict(action='open', id=photo_id), self.config)
            item = next(p for p in reply['photos'] if p['id'] == photo_id)
            self.assertNotEqual(str(self.library.original(photo)), item['display'])
            self.assertEqual(self.library.original(photo).read_bytes(), data)
            with Image.open(item['display']) as image:
                self.assertEqual(image.size, (64, 96) if path.name.startswith('Rotated') else (96, 64))
                self.assertEqual((reply['detail']['width'], reply['detail']['height']), image.size)
                self.assertIsNotNone(image.convert('RGB').getbbox())
                if path.name == 'Still.heic':
                    self.assertEqual(image.mode, 'RGBA')
                    self.assertLess(abs(image.getpixel((75, 20))[3] - 110), 2)
                    self.assertGreater(image.getpixel((20, 20))[0], image.getpixel((75, 20))[0] + 80)
                if path.suffix == '.dng':
                    self.assertGreater(len({image.getpixel((x, y)) for x in range(image.width) for y in range(image.height)}), 100)
            exported = self.library.export(photo_id, self.root / 'export')
            self.assertEqual(Path(exported).read_bytes(), data)
            self.assertEqual(path.read_bytes(), data)

    def test_invalid_codec_keeps_original_and_no_partial_display_or_temp_files(self):
        path = self.source / 'Broken.cr3'
        path.write_bytes(b'not a RAW camera file')
        self.library.import_folders([self.source.parent])
        photo_id = digest_file(path)
        with self.assertRaises(LibraryError):
            process(dict(action='open', id=photo_id), self.config)
        self.assertEqual(self.library.original(self.library.photo(photo_id)).read_bytes(), path.read_bytes())
        self.assertEqual(list((self.library.root / 'private/display').iterdir()), [])

    def test_hidden_decoder_does_not_run_locked_or_return_a_concurrent_hidden_path(self):
        path = self.source / 'Hidden.heic'
        heic(path)
        self.library.import_folders([self.source.parent])
        photo_id = digest_file(path)
        self.library.edit(photo_id, 'hidden_set', '1')
        with patch.object(decoding, 'display', side_effect=AssertionError('Locked decoder ran')):
            result = process(dict(action='open', id=photo_id), self.config)
        self.assertIn('Hidden is locked', result['error'])
        self.assertEqual(result['photos'], [])
        self.library.edit(photo_id, 'hidden_set', '0')
        original = decoding.display
        def hide(library, photo):
            result = original(library, photo)
            library.edit(photo_id, 'hidden_set', '1')
            return result
        with patch.object(decoding, 'display', side_effect=hide):
            with self.assertRaises(LibraryError):
                process(dict(action='open', id=photo_id), self.config)
        self.assertEqual(process({}, self.config)['photos'], [])

    def test_thumbnail_size_and_helper_arguments(self):
        helper = Path(__file__).resolve().parents[1] / 'build/photo-decoder/photo_decode'
        result = subprocess.run([str(helper), 'heif', '/missing', '/missing', '999'], capture_output=True)
        self.assertEqual(result.returncode, 2)
        path = self.source / 'Camera.dng'
        dng(path)
        dimensions = decoding.decode(path, '.dng', self.root / 'thumb.png', True)
        self.assertEqual(dimensions, dict(width=96, height=64))
        with Image.open(self.root / 'thumb.png') as image:
            self.assertLessEqual(max(image.size), 640)

    def test_existing_import_gains_a_thumbnail_without_reimporting_original(self):
        path = self.source / 'Older.heic'
        heic(path, 6)
        self.library.import_folders([self.source.parent])
        photo_id = digest_file(path)
        self.library.thumbnail(photo_id).unlink()
        self.library.db.execute('UPDATE photos SET width=0,height=0,dirty=0 WHERE id=?', (photo_id,))
        self.library.db.commit()
        result = self.library.import_folders([self.source.parent])
        self.assertEqual(result['unchanged'], 1)
        self.assertTrue(self.library.thumbnail(photo_id).is_file())
        photo = self.library.photo(photo_id)
        self.assertEqual((photo['width'], photo['height'], photo['dirty']), (64, 96, 1))
        self.assertEqual(digest_file(self.library.original(photo)), photo_id)
        legacy = request(dict(action='open', id=photo_id), self.config)
        old_item = next(p for p in legacy['photos'] if p['id'] == photo_id)
        self.assertNotIn('display', old_item)
        with Image.open(old_item['original']) as image:
            self.assertEqual(image.size, (64, 96))
