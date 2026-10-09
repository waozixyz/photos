import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from photos import live
from photos.export import Export
from photos.library import Library, LibraryError, digest_file
from photos.worker import process as request


def movie(path):
    subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-f', 'lavfi', '-i',
                    'testsrc2=size=96x64:rate=12', '-f', 'lavfi', '-i', 'sine=frequency=330',
                    '-t', '2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(path)],
                   check=True, capture_output=True, timeout=15)


def process(value, config):
    return request({**value, 'gallery_format': 2}, config)


class LiveTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / 'source/Camera'
        self.source.mkdir(parents=True)
        self.still, self.motion = self.source / 'Moment.jpg', self.source / 'Moment.mov'
        Image.new('RGB', (96, 64), (190, 70, 30)).save(self.still)
        movie(self.motion)
        self.originals = {p: p.read_bytes() for p in [self.still, self.motion]}
        self.library = Library(self.root / 'library')
        self.addCleanup(self.library.db.close)
        self.library.import_folders([self.source.parent])
        self.still_id, self.motion_id = digest_file(self.still), digest_file(self.motion)
        self.config = dict(root=str(self.library.root))

    def test_pair_gallery_search_album_motion_playback_and_originals(self):
        result = process({}, self.config)
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['total'], 2)
        self.assertEqual(result['photos'][0]['id'], self.still_id)
        self.assertEqual(result['photos'][0]['kind'], 'live')
        self.assertEqual(result['albums'][0]['count'], 1)
        self.assertEqual(self.library.query(search='type:live')['count'], 1)
        self.assertEqual(self.library.query(search='type:video')['count'], 0)
        reply = process(dict(action='open', id=self.still_id), self.config)
        self.assertEqual(reply['detail']['size'], sum(len(b) for b in self.originals.values()))
        play = process(dict(action='playback_check', id=self.still_id), self.config)
        self.assertEqual(play['playback_id'], self.still_id)
        self.assertEqual(Path(play['playback_path']).read_bytes(), self.originals[self.motion])
        self.assertTrue(all(p.read_bytes() == data for p, data in self.originals.items()))

    def test_pair_mutations_are_atomic_and_protect_both_components(self):
        for action, value, field, expected in [('favorite', None, 'favorite', 1),
                                              ('caption', 'Our moment', 'caption', 'Our moment'),
                                              ('tags', 'family', 'tags', 'family'),
                                              ('archived_set', '1', 'archived', 1)]:
            self.library.edit(self.still_id, action, value)
            for photo_id in [self.still_id, self.motion_id]:
                self.assertEqual(self.library.photo(photo_id)[field], expected)
        with self.assertRaises(LibraryError):
            self.library.edit(self.still_id, 'favorite', authorize=lambda p: p['id'] != self.motion_id)
        self.assertEqual(self.library.photo(self.still_id)['favorite'], 1)
        self.library.edit(self.still_id, 'taken', '2024-03-05T12:00:00Z')
        self.assertEqual(self.library.photo(self.still_id)['taken'], self.library.photo(self.motion_id)['taken'])
        self.library.edit(self.still_id, 'hidden_set', '1')
        self.assertEqual(process({}, self.config)['photos'], [])
        self.assertIn('error', process(dict(action='playback_check', id=self.still_id), self.config))
        self.library.edit(self.motion_id, 'hidden_set', '0')
        self.library.edit(self.still_id, 'trash')
        self.assertEqual(self.library.query('trash')['count'], 1)
        with self.assertRaises(LibraryError):
            process(dict(action='playback_check', id=self.still_id), self.config)
        self.library.edit(self.still_id, 'restore')
        self.assertEqual(self.library.query('trash')['count'], 0)

    def test_both_export_modes_reimport_pair_and_separation_survives_rescan(self):
        output = self.root / 'export'
        self.library.export(self.still_id, output)
        for original in [self.still, self.motion]:
            self.assertEqual((output / original.name).read_bytes(), original.read_bytes())
        mirror = self.root / 'mirror'
        with Export(self.library, str(mirror)) as export:
            export.run()
        for destination in [output, mirror]:
            restored = Library(self.root / ('restored-' + destination.name))
            try:
                restored.import_folders([destination])
                self.assertEqual(restored.query()['count'], 1)
                self.assertEqual(restored.query()['total'], 2)
                self.assertEqual(len(live.components(restored, restored.photo(self.still_id))), 2)
            finally:
                restored.db.close()
        process(dict(action='live_separate', id=self.still_id), self.config)
        self.library.import_folders([self.source.parent])
        self.assertEqual(self.library.query()['count'], 2)
        self.assertEqual(self.library.query(search='type:live')['count'], 0)
        self.assertTrue(all(p.read_bytes() == data for p, data in self.originals.items()))

    def test_partial_sync_or_child_privacy_change_exposes_no_gallery_or_playback_path(self):
        self.library.db.execute('UPDATE photos SET hidden=1 WHERE id=?', (self.motion_id,))
        self.library.db.commit()
        self.assertEqual(process({}, self.config)['photos'], [])
        self.assertEqual(self.library.album_list()[0]['count'], 0)
        with self.assertRaises(LibraryError):
            process(dict(action='playback_check', id=self.still_id), self.config)
        with self.assertRaises(LibraryError):
            process(dict(action='open', id=self.still_id), self.config)
        self.library.db.execute('DELETE FROM photos WHERE id=?', (self.motion_id,))
        self.library.db.commit()
        self.assertEqual(process({}, self.config)['photos'], [])

    def test_ambiguous_names_long_movies_or_different_days_stay_independent(self):
        live.separate(self.library, self.still_id, lambda p: True)
        self.library.db.execute("UPDATE photos SET edits='{}',metadata='{}'")
        self.library.db.commit()
        with patch('photos.video.probe', return_value=dict(duration_ms=11000)):
            live.detect(self.library)
        self.assertEqual(self.library.query()['count'], 2)
        old = 86400 * 3
        self.library.db.execute('UPDATE photos SET taken=taken-? WHERE id=?', (old, self.motion_id))
        self.library.db.execute('UPDATE sources SET mtime=mtime-? WHERE photo_id=?', (old * 1000000000, self.motion_id))
        self.library.db.commit()
        live.detect(self.library)
        self.assertEqual(self.library.query()['count'], 2)
        self.library.db.execute('UPDATE photos SET taken=taken+? WHERE id=?', (old, self.motion_id))
        self.library.db.execute('UPDATE sources SET mtime=mtime+? WHERE photo_id=?', (old * 1000000000, self.motion_id))
        self.library.db.commit()
        Image.new('RGB', (96, 64), 'blue').save(self.still.with_suffix('.jpeg'))
        self.library.import_folders([self.source.parent])
        self.assertEqual(self.library.query()['count'], 3)

    def test_edited_still_and_motion_copies_are_independent_and_keep_pair_originals(self):
        from photos import editing, video
        still_copy = editing.save(self.library, self.still_id, [90, 0, 0, 100, 100, 100, 0])
        motion_copy = video.save(self.library, self.motion_id, [0, 2000, 90, 0])
        for photo_id in [still_copy, motion_copy]:
            self.assertEqual(len(live.components(self.library, self.library.photo(photo_id))), 1)
        self.assertEqual(len(live.components(self.library, self.library.photo(self.still_id))), 2)
        self.assertEqual(self.library.query()['count'], 3)
        self.assertTrue(all(p.read_bytes() == data for p, data in self.originals.items()))
