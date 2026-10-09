from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from photos import guest, live, query, vault
from photos.library import Library, LibraryError, digest_file, packed
from photos.worker import process


class StorageTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source/Storage'
        self.source.mkdir(parents=True)
        self.library = Library(self.root / 'library')
        self.addCleanup(self.library.db.close)
        self.config = dict(root=str(self.library.root))
        self.originals = {}

    def add(self, name, size, backed=True, **state):
        path = self.source / name
        if path.suffix == '.mp4':
            path.write_bytes(('Synthetic movie ' + name).encode())
        else:
            Image.new('RGB', (30 + len(self.originals), 40), 'blue').save(path)
        self.originals[path] = path.read_bytes()
        self.library.import_folders([self.source.parent])
        identity = digest_file(path)
        values = dict(size=size, manifest=packed(dict(chunks=['synthetic-encrypted-chunk'])) if backed else '{}', **state)
        for key, value in values.items():
            self.library.db.execute(f'UPDATE photos SET {key}=? WHERE id=?', (value, identity))
        self.library.db.commit()
        return identity

    def page(self, **values):
        return self.library.query('large', **values)

    def safe(self):
        for path, data in self.originals.items():
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(self.library.original(self.library.photo(digest_file(path))).read_bytes(), data)

    def test_threshold_backed_only_archived_and_exact_64_bit_size_order(self):
        self.add('Boundary.png', 10485760)
        self.add('Pending.png', 6 * 2**30, backed=False)
        small = self.add('Small.png', 10485761)
        photo = self.add('Archived.png', 3 * 2**30 + 1, archived=1)
        movie = self.add('Big.mp4', 5 * 2**30 + 19)
        result = self.page(sort='oldest')
        self.assertEqual([p['id'] for p in result['photos']], [movie, photo, small])
        self.assertEqual([p['storage_size'] for p in result['photos']], [5 * 2**30 + 19, 3 * 2**30 + 1, 10485761])
        self.assertEqual(result['count'], 3)
        self.safe()

    def test_filters_search_and_stable_paging(self):
        first = self.add('First.png', 20 * 2**20, taken=100)
        second = self.add('Second.png', 20 * 2**20, taken=200)
        movie = self.add('Movie.mp4', 30 * 2**20)
        self.assertEqual([p['id'] for p in self.page(limit=1)['photos']], [movie])
        self.assertEqual([p['id'] for p in self.page(offset=1, limit=1)['photos']], [second])
        self.assertEqual([p['id'] for p in self.page(offset=2, limit=1)['photos']], [first])
        self.assertEqual(self.page(offset=3)['photos'], [])
        self.assertEqual(self.page(large_type='photos')['count'], 2)
        self.assertEqual(self.page(large_type='videos')['photos'][0]['id'], movie)
        self.assertEqual(self.page(search='First')['count'], 1)
        for invalid in ['other', 'all OR 1=1', 1]:
            with self.assertRaises(LibraryError):
                self.page(large_type=invalid)
        self.safe()

    def test_hidden_trash_excluded_even_when_hidden_access_is_authorized(self):
        self.add('Hidden.png', 20 * 2**20, hidden=1)
        self.add('Trashed.png', 30 * 2**20, trashed=1)
        visible = self.add('Visible.png', 15 * 2**20)
        self.assertEqual([p['id'] for p in self.page(hidden_access=True)['photos']], [visible])
        self.assertEqual(self.page(search='Hidden')['count'], 0)
        self.assertEqual(self.page(search='Trashed')['count'], 0)
        self.safe()

    def test_paired_originals_count_once_and_require_both_backups_and_privacy(self):
        still = self.add('Still.png', 6 * 2**20)
        motion = self.add('Motion.mp4', 7 * 2**20)
        self.assertTrue(live.pair(self.library, still, motion))
        result = self.page()
        self.assertEqual([(p['id'], p['storage_size']) for p in result['photos']], [(still, 13 * 2**20)])
        self.assertEqual(self.page(large_type='photos')['count'], 1)
        self.assertEqual(self.page(large_type='videos')['count'], 0)
        self.library.db.execute("UPDATE photos SET manifest='{}' WHERE id=?", (motion,))
        self.library.db.commit()
        self.assertEqual(self.page()['count'], 0)
        self.library.db.execute('UPDATE photos SET manifest=?,hidden=1 WHERE id=?', (packed(dict(chunks=['fixture'])), motion))
        self.library.db.commit()
        self.assertEqual(self.page(hidden_access=True)['photos'], [])
        self.safe()

    def test_gallery_hydration_holds_writer_lock_and_reports_only_small_fields(self):
        identity = self.add('Visible.png', 20 * 2**20)
        original = query.page

        def locked(library, **request):
            writer = Library(library.root)
            try:
                writer.db.execute('PRAGMA busy_timeout=0')
                with self.assertRaisesRegex(Exception, 'locked'):
                    writer.db.execute('UPDATE photos SET hidden=1 WHERE id=?', (identity,))
            finally:
                writer.db.close()
            return original(library, **request)

        with patch('photos.query.page', side_effect=locked):
            result = process(dict(view='large', gallery_format=2), self.config)
        self.assertFalse(self.library.db.in_transaction)
        self.assertEqual(result['photos'][0]['storage_size'], 20 * 2**20)
        self.assertNotIn('manifest', result['photos'][0])
        self.assertNotIn('metadata', result['photos'][0])
        self.safe()

    def test_app_lock_guest_scope_and_recoverable_batch_trash(self):
        first = self.add('First.png', 20 * 2**20)
        second = self.add('Second.png', 30 * 2**20)
        result = process(dict(action='trash', ids=[first, second], view='large'), self.config)
        self.assertFalse(result['photos'])
        self.assertEqual(self.library.query('trash')['count'], 2)
        self.library.edit_many([first, second], 'restore')
        self.assertEqual(self.page()['count'], 2)
        password = 'synthetic-storage-password'
        owner = vault.authenticate(self.library, password, setup=True)
        self.addCleanup(vault.lock, self.library, owner)
        guest.start(self.library, dict(ids=[first]))
        result = process(dict(view='large', large_type='videos', gallery_format=2), self.config)
        self.assertEqual([p['id'] for p in result['photos']], [first])
        self.assertNotIn('storage_size', result['photos'][0])
        process(dict(action='guest_exit', value=password), self.config)
        process(dict(action='app_lock_enable', value=password), self.config)
        result = process(dict(view='large', gallery_format=2), self.config)
        self.assertEqual(result['photos'], [])
        self.assertEqual(result['app_locked'], 1)
        self.safe()
