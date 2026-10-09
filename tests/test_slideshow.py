from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from photos import people, slideshow, live
from photos.library import Library, LibraryError, digest_file
from photos.worker import process
from test_people import vector


class SlideshowTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); source = self.root / 'source/Family'; source.mkdir(parents=True)
        self.ids = []; self.originals = {}
        for i in range(72):
            path = source / f'photo{i}.png'
            Image.new('RGB', (30+i, 40), (i*3, 80, 120)).save(path)
            self.ids.append(digest_file(path)); self.originals[path] = path.read_bytes()
        self.library = Library(self.root / 'library'); self.addCleanup(self.library.db.close)
        self.library.import_folders([source.parent])
        for i, photo_id in enumerate(self.ids):
            self.library.edit(photo_id, 'taken', f'{2020+i//6}-07-{1+i%6:02d}T12:00:00Z')
        self.config = dict(root=str(self.library.root))
        self.base = dict(action='slide_frame', view='photos', album='Family', slide_seed=345)

    def test_paging_wrap_shuffle_and_unchanged_originals(self):
        first = process(self.base, self.config)
        self.assertEqual(first['slide_id'], self.ids[0]); self.assertEqual(first['slide_count'], 72)
        self.assertEqual(process({**self.base, 'slide_offset': 65}, self.config)['slide_id'], self.ids[65])
        self.assertEqual(process({**self.base, 'slide_offset': 72}, self.config)['slide_id'], self.ids[0])
        order = [process({**self.base, 'slide_shuffle': 1, 'slide_offset': i}, self.config)['slide_id'] for i in range(72)]
        self.assertEqual(set(order), set(self.ids)); self.assertNotEqual(order, self.ids)
        self.assertEqual(process({**self.base, 'slide_shuffle': 1, 'slide_offset': 65}, self.config)['slide_id'], order[65])
        second = [process({**self.base, 'slide_shuffle': 1, 'slide_seed': 346, 'slide_offset': i}, self.config)['slide_id'] for i in range(3)]
        self.assertNotEqual(second, order[:3])
        for path, data in self.originals.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertEqual(self.library.query()['total'], 72)

    def test_album_union_smart_rules_live_safety_and_video_exclusion(self):
        self.library.edit_album('', 'create', 'Second')
        self.library.edit(self.ids[0], 'album_add', 'Second')
        self.library.edit_album('', 'smart_create', 'Recent', search='year:2021')
        request = {**self.base, 'view': 'albums', 'album': '', 'slide_albums': ['Second', 'Recent', 'Second']}
        self.assertEqual(process(request, self.config)['slide_count'], 7)
        self.library.edit(self.ids[1], 'hidden_set', '1')
        self.library.edit(self.ids[2], 'trash')
        self.library.db.execute("UPDATE photos SET kind='video' WHERE id=?", (self.ids[3],)); self.library.db.commit()
        self.assertTrue(live.pair(self.library, self.ids[0], self.ids[3]))
        self.assertEqual(process(self.base, self.config)['slide_count'], 69)
        self.library.db.execute('UPDATE photos SET hidden=1 WHERE id=?', (self.ids[3],)); self.library.db.commit()
        with self.assertRaises(LibraryError):
            process({**self.base, 'action':'slide_check', 'id':self.ids[0]}, self.config)
        self.library.db.execute('UPDATE photos SET hidden=0 WHERE id=?', (self.ids[3],)); self.library.db.commit()
        self.library.edit_album('Family', 'archived')
        self.assertEqual(process(self.base, self.config)['slide_count'], 69)
        self.library.edit(self.ids[0], 'album_remove', 'Second')
        with self.assertRaises(LibraryError):
            process({**request, 'action': 'slide_check', 'id': self.ids[0]}, self.config)

    def test_hidden_trash_membership_and_slow_materialization_revoke_access(self):
        result = process(self.base, self.config)
        check = {**self.base, 'action': 'slide_check', 'id': result['slide_id']}
        self.assertTrue(process(check, self.config)['slide_checked'])
        self.assertNotIn('slide_path', process(check, self.config))
        self.library.edit(result['slide_id'], 'hidden_set', '1')
        with self.assertRaises(LibraryError): process(check, self.config)
        self.library.edit(result['slide_id'], 'hidden_set', '0')
        original = slideshow.background
        def hide_after_decode(library, photo):
            asset = original(library, photo)
            self.library.edit(photo['id'], 'trash')
            return asset
        with patch.object(slideshow, 'background', hide_after_decode):
            with self.assertRaises(LibraryError): process({**self.base, 'slide_blur': 1}, self.config)
        self.assertFalse(self.library.db.in_transaction)
        self.assertEqual(self.library.query()['total'], 72)

    def test_memory_lane_plays_all_eligible_years_and_revokes_ignored_person(self):
        self.library.set_setting('people_enabled', True); self.library.set_setting('people_processing', True)
        for photo_id in self.ids[:18]:
            people.store(self.library, photo_id, [dict(rect=[0,0,3000,3000], embedding=vector())], self.root)
        person = self.library.db.execute('SELECT person_id FROM faces LIMIT 1').fetchone()[0]
        self.library.set_setting('people_processing', False); people.edit(self.library, 'person_name', person, 'Ada')
        people.edit(self.library, 'person_birth', person, '2018-06-01')
        request = {**self.base, 'view': 'lane', 'album': '', 'person': person}
        first = process(request, self.config)
        self.assertEqual(first['slide_id'], self.ids[6]); self.assertEqual(first['slide_count'], 12)
        self.assertIn('turned 3', first['slide_caption'])
        self.assertEqual(process({**request, 'slide_offset': 6}, self.config)['slide_id'], self.ids[12])
        for photo_id in self.ids[6:9]: self.library.edit(photo_id, 'hidden_set', '1')
        with self.assertRaises(LibraryError): process({**request, 'action':'slide_check', 'id':first['slide_id']}, self.config)
        with self.assertRaises(LibraryError): process(request, self.config)
        people.edit(self.library, 'person_ignore', person)
        with self.assertRaises(LibraryError): process(request, self.config)

    def test_invalid_selections_and_background_are_bounded_and_private(self):
        result = process({**self.base, 'slide_blur':1}, self.config)
        path = Path(result['slide_background'])
        self.assertTrue(path.is_relative_to(self.library.root / 'private'))
        with Image.open(path) as image: self.assertLessEqual(max(image.size), 1024)
        for changes in [dict(view='hidden'), dict(view='trash'), dict(slide_offset=-1), dict(slide_seed=True),
                        dict(slide_albums=['Family']*65), dict(slide_albums='Family'), dict(slide_albums=['missing'])]:
            with self.assertRaises(LibraryError): process({**self.base, **changes}, self.config)
        for path, data in self.originals.items(): self.assertEqual(path.read_bytes(), data)
