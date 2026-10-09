import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from photos.library import Library, digest_file
from photos.export import Export, destination
from photos.worker import library_request, daemon


class ExportTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'Camera'
        self.source.mkdir()
        self.path = self.source / 'photo.jpg'
        Image.new('RGB', (100, 80), 'blue').save(self.path)
        self.library = Library(self.root / 'library')
        self.addCleanup(self.library.db.close)
        self.library.import_folders([self.source])
        self.photo_id = digest_file(self.path)
        self.output = self.root / 'export'

    def run_export(self):
        with Export(self.library, str(self.output), [self.source]) as export:
            return export.run()

    def test_incremental_album_metadata_membership_rename_and_trash_mirror(self):
        self.library.edit(self.photo_id, 'album_add', 'Other/album')
        self.library.edit_album('', 'create', 'Empty')
        self.library.edit_album('Empty', 'description', 'Kept empty')
        first = self.run_export()
        originals = sorted(self.output.rglob('*.jpg'))
        self.assertEqual(len(originals), 2)
        self.assertTrue(all(p.read_bytes() == self.path.read_bytes() for p in originals))
        self.assertEqual(len(list(self.output.rglob('.photos-album.json'))), 3)
        recovered = Library(self.root / 'recovered')
        self.addCleanup(recovered.db.close)
        recovered.import_folders([self.output])
        self.assertEqual(recovered.query()['total'], 1)
        self.assertEqual(json.loads(recovered.photo(self.photo_id)['albums']), ['Camera', 'Other/album'])
        self.assertEqual(recovered.album_details('Empty')['description'], 'Kept empty')
        mtimes = [p.stat().st_mtime_ns for p in originals]
        self.assertEqual(self.run_export()['written'], 0)
        self.library.edit(self.photo_id, 'caption', 'Changed caption')
        self.library.edit(self.photo_id, 'tags', 'coast')
        self.library.edit(self.photo_id, 'taken', '2024-06-15T14:30:00Z')
        self.run_export()
        self.assertEqual([p.stat().st_mtime_ns for p in originals], mtimes)
        metadata = [json.loads(p.read_bytes()) for p in self.output.rglob('*.jpg.json')]
        self.assertTrue(all(m['description'] == 'Changed caption' and m['tags'] == 'coast' for m in metadata))
        unrelated = self.output / 'notes.txt'
        unrelated.write_text('Keep me')
        self.library.rename_album('Camera', 'New camera')
        self.run_export()
        self.assertTrue(any('New camera' in str(p) for p in self.output.rglob('*.jpg')))
        self.assertTrue(all('Camera--' not in str(p) for p in self.output.rglob('*.jpg')))
        self.library.edit(self.photo_id, 'trash')
        self.run_export()
        self.assertEqual(list(self.output.rglob('*.jpg')), [])
        self.assertEqual(unrelated.read_text(), 'Keep me')
        self.assertEqual(self.path.read_bytes(), self.library.original(self.library.photo(self.photo_id)).read_bytes())
        self.library.edit(self.photo_id, 'restore')
        self.run_export()
        self.assertEqual(len(list(self.output.rglob('*.jpg'))), 2)

    def test_outside_edits_and_symlinks_are_preserved_and_sources_cannot_overlap(self):
        self.run_export()
        original = next(self.output.rglob('*.jpg'))
        original.write_bytes(b'An outside edit')
        with self.assertRaises(ValueError):
            self.run_export()
        self.library.edit(self.photo_id, 'hidden')
        with self.assertRaises(ValueError):
            self.run_export()
        self.assertEqual(original.read_bytes(), b'An outside edit')
        for folder in [self.library.root, self.library.root / 'exports', self.source, self.source / 'exports', self.root]:
            with self.assertRaises(ValueError):
                destination(self.library, str(folder), [self.source])
        original.unlink()
        original.symlink_to(self.path)
        with self.assertRaises(ValueError):
            self.run_export()
        self.assertTrue(original.is_symlink())

    def test_background_export_requests_complete_and_stop_preserves_copy(self):
        config = dict(root=str(self.library.root), sources=[str(self.source)])
        library_request(self.library, dict(action='export_continuous', value=str(self.output)), config)
        daemon(config, once=True)
        self.assertFalse(self.library.setting('export')['requested'])
        self.assertTrue(self.library.setting('export')['continuous'])
        self.assertTrue(list(self.output.rglob('*.jpg')))
        library_request(self.library, dict(action='export_stop'), config)
        self.assertFalse(self.library.setting('export')['continuous'])
        self.assertTrue(list(self.output.rglob('*.jpg')))
        with self.assertRaises(ValueError):
            library_request(self.library, dict(action='import', value=str(self.output)), config)

    def test_interrupted_metadata_publication_resumes_without_rewriting_media(self):
        self.run_export()
        original = next(self.output.rglob('*.jpg'))
        mtime = original.stat().st_mtime_ns
        self.library.edit(self.photo_id, 'caption', 'A resumed caption')
        with patch('photos.export.os.replace', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                self.run_export()
        self.run_export()
        self.assertEqual(original.stat().st_mtime_ns, mtime)
        self.assertEqual(json.loads(next(self.output.rglob('*.jpg.json')).read_bytes())['description'], 'A resumed caption')

    def test_cancelled_export_preserves_existing_copy_and_a_new_request(self):
        self.run_export()
        original = next(self.output.rglob('*.jpg'))
        self.library.edit(self.photo_id, 'trash')
        with Export(self.library, str(self.output), [self.source], cancelled=lambda: True) as export:
            with self.assertRaises(ValueError):
                export.run()
        self.assertTrue(original.is_file())
        config = dict(root=str(self.library.root), sources=[str(self.source)])
        library_request(self.library, dict(action='export_continuous', value=str(self.output)), config)
        run = Export.run
        def replaced(export):
            result = run(export)
            library_request(self.library, dict(action='export_start', value=str(self.output)), config)
            return result
        with patch.object(Export, 'run', replaced):
            daemon(config, once=True)
        self.assertTrue(self.library.setting('export')['requested'])
        self.assertFalse(self.library.setting('export')['continuous'])


if __name__ == '__main__':
    unittest.main()
