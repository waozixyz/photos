import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from photos import collage, live
from photos.library import Library, LibraryError, digest_file, packed
from photos.sync import Client
from photos.worker import process


class CollageTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.source = self.root / 'source/Family'; self.source.mkdir(parents=True)
        self.ids = []; self.originals = {}
        for i in range(6):
            path = self.source / f'Photo {i}.png'
            image = Image.new('RGBA', (120 + i, 80), (20 + i * 30, 50, 120, 255))
            if i == 0:
                for x in range(60):
                    for y in range(80): image.putpixel((x, y), (220, 20, 10, 255))
            image.save(path); self.ids.append(digest_file(path)); self.originals[path] = path.read_bytes()
        self.library = Library(self.root / 'library'); self.addCleanup(self.library.db.close)
        self.library.import_folders([self.source.parent]); self.config = dict(root=str(self.library.root))

    def spec(self, count=2, layout=0, shape=0):
        return dict(layout=layout, shape=shape, items=[dict(id=i, cx=5000, cy=5000, zoom=100) for i in self.ids[:count]])

    def test_native_grid_shape_framing_swap_and_immutable_copy(self):
        begin = process(dict(action='collage_begin', ids=self.ids[:2]), self.config)
        self.assertEqual([i['id'] for i in begin['collage_photos']], self.ids[:2])
        spec = self.spec(); first = collage.save(self.library, spec, self.config)
        photo = self.library.photo(first)
        self.assertEqual((photo['width'], photo['height']), (2048, 2048))
        self.assertEqual(self.library.query(album='Collages')['count'], 1)
        with Image.open(self.library.original(photo)) as image:
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
            self.assertEqual(image.getpixel((1536, 1024)), (50, 50, 120))
        spec['items'][0].update(cx=0, zoom=200)
        cropped = collage.save(self.library, spec, self.config)
        with Image.open(self.library.original(self.library.photo(cropped))) as image:
            self.assertEqual(image.getpixel((512, 1024)), (220, 20, 10))
        spec['items'].reverse(); spec['shape'] = 1
        swapped = collage.save(self.library, spec, self.config)
        with Image.open(self.library.original(self.library.photo(swapped))) as image:
            self.assertEqual(image.size, (2048, 1536))
            self.assertEqual(image.getpixel((512, 768)), (50, 50, 120))
        self.library.edit(first, 'caption', 'Keep later edits'); self.library.edit(first, 'trash')
        self.assertEqual(collage.save(self.library, self.spec(), self.config), first)
        self.assertEqual(self.library.photo(first)['caption'], 'Keep later edits')
        self.assertTrue(self.library.photo(first)['trashed'])
        for path, data in self.originals.items(): self.assertEqual(path.read_bytes(), data)

    def test_all_counts_layouts_and_portrait_render_without_empty_tiles(self):
        program = Path(__file__).resolve().parents[1] / 'build/image-editor/image_edit'
        output = self.root / 'layout.png'
        # Exercise the shared native layout for every count and layout. The
        # service supplies trusted original paths rather than client paths.
        for count in range(2, 7):
            for layout in range(4):
                spec = self.spec(count, layout, 2)
                for item in spec['items']: item['path'] = str(self.library.original(self.library.photo(item['id'])))
                result = subprocess.run([str(program), 'collage', str(output)], input=packed(spec).encode(), capture_output=True)
                self.assertEqual(result.returncode, 0, (count, layout, result.stderr))
                with Image.open(output) as image:
                    self.assertEqual(image.size, (1536, 2048))
                    colors = {color for pixels, color in image.getcolors(256)}
                    for color in [(20+i*30,50,120) for i in range(1,count)]:
                        self.assertIn(color, colors)

    def test_exif_orientation_transparency_and_export_encrypted_restore(self):
        oriented = self.source / 'Rotated.jpg'; image = Image.new('RGB', (80, 120), (190, 50, 30))
        exif = image.getexif(); exif[274] = 6; image.save(oriented, exif=exif)
        transparent = self.source / 'Transparent.png'; Image.new('RGBA', (100, 100), (255, 0, 0, 0)).save(transparent)
        self.library.import_folders([self.source.parent]); ids = [digest_file(oriented), digest_file(transparent)]
        sources = collage.prepare(self.library, ids, self.config)
        self.assertEqual((sources[0]['width'], sources[0]['height']), (120, 80))
        spec = dict(layout=1, shape=0, items=[dict(id=i, cx=5000, cy=5000, zoom=100) for i in ids])
        saved = process(dict(action='collage_save', value=packed(spec)), self.config)['saved_copy']
        photo = self.library.photo(saved)
        with Image.open(self.library.original(photo)) as image:
            self.assertEqual(image.getpixel((1536, 1024)), (255, 255, 255))
        export = self.root / 'export'; self.library.export(saved, export)
        imported = Library(self.root / 'imported'); self.addCleanup(imported.db.close); imported.import_folders([export])
        self.assertEqual(json.loads(imported.photo(saved)['metadata'])['photosCollage'], spec)
        sender = Client(self.library, dict(node='http://127.0.0.1:18080', node_id='a'*64))
        receiver = Client(imported, dict(node='http://127.0.0.1:18080', node_id='a'*64), sender.identity_path)
        record = sender.record(photo, manifest={})
        self.assertNotIn('photosCollage', json.dumps(record))
        receiver.accept(record); imported.db.commit()
        self.assertEqual(json.loads(imported.photo(saved)['metadata'])['photosCollage'], spec)

    def test_private_selections_animations_videos_and_live_motion_fail_atomically(self):
        self.library.edit(self.ids[1], 'hidden_set', '1')
        with self.assertRaises(LibraryError): collage.prepare(self.library, self.ids[:2], self.config)
        with self.assertRaises(LibraryError): collage.save(self.library, self.spec(), self.config)
        self.library.edit(self.ids[1], 'hidden_set', '0'); self.library.edit(self.ids[1], 'trash')
        with self.assertRaises(LibraryError): collage.check(self.library, self.spec())
        self.library.edit(self.ids[1], 'restore')
        animated = self.source / 'Animated.gif'
        Image.new('RGB',(10,10),'red').save(animated,save_all=True,append_images=[Image.new('RGB',(10,10),'blue')],duration=100,loop=0)
        motion = self.source / 'Motion.mp4'; motion.write_bytes(b'Synthetic motion')
        self.library.import_folders([self.source.parent]); motion_id = digest_file(motion)
        for photo_id in [digest_file(animated), motion_id]:
            with self.assertRaises(LibraryError): collage.prepare(self.library,[self.ids[0],photo_id],self.config)
        live.pair(self.library, self.ids[0], motion_id)
        self.assertEqual(len(collage.prepare(self.library, self.ids[:2], self.config)), 2)
        self.library.db.execute('UPDATE photos SET hidden=1 WHERE id=?',(motion_id,)); self.library.db.commit()
        with self.assertRaises(LibraryError): collage.check(self.library,self.spec())
        self.assertFalse(self.library.db.execute("SELECT 1 FROM photos WHERE name='Collage.png'").fetchone())

    def test_concurrent_hide_after_render_and_process_failures_publish_no_copy(self):
        native_render = collage.render
        def hide(*args):
            result = native_render(*args)
            other = Library(self.library.root)
            try: other.edit(self.ids[1], 'hidden_set', '1')
            finally: other.db.close()
            return result
        with patch.object(collage, 'render', hide):
            with self.assertRaises(LibraryError): collage.save(self.library, self.spec(), self.config)
        self.assertFalse(self.library.db.execute("SELECT 1 FROM photos WHERE name='Collage.png'").fetchone())
        self.library.edit(self.ids[1], 'hidden_set', '0')
        with patch.object(collage.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fixture',65)):
            with self.assertRaises(LibraryError): collage.save(self.library,self.spec(),self.config)
        self.assertEqual(list((self.library.root/'private/collage').iterdir()), [])

    def test_invalid_specifications_and_client_paths_never_modify_originals(self):
        for change in [dict(layout=True),dict(shape=4),dict(items=[]),dict(items=self.spec()['items']*2),dict(items=[{}]),dict(items='bad')]:
            with self.assertRaises(LibraryError): collage.specification({**self.spec(),**change})
        for key, value in [('zoom',99),('zoom',401),('cx',-1),('cy',10001),('zoom',True)]:
            spec=self.spec();spec['items'][0][key]=value
            with self.assertRaises(LibraryError):collage.specification(spec)
        spec=self.spec();spec['items'][0]['path']='/etc/passwd'
        self.assertNotIn('path',collage.specification(spec)['items'][0])
        for path,data in self.originals.items():self.assertEqual(path.read_bytes(),data)

    def test_smart_or_deleted_album_collision_keeps_definitions_and_saved_copy_edits(self):
        self.library.edit_album('', 'smart_create', 'Collages', search='tag:family')
        saved = process(dict(action='collage_save', value=packed(self.spec())), self.config)
        self.assertEqual(saved['album'], 'Collages 2')
        self.assertEqual(self.library.album_details('Collages')['filter'], 'tag:family')
        self.library.edit(saved['saved_copy'], 'album_remove', 'Collages 2')
        self.library.edit_album('Collages 2', 'delete_empty')
        second = process(dict(action='collage_save', value=packed(self.spec(shape=1))), self.config)
        self.assertEqual(second['album'], 'Collages 3')
        repeated = process(dict(action='collage_save', value=packed(self.spec())), self.config)
        self.assertEqual(repeated['saved_copy'], saved['saved_copy'])
        self.assertEqual(repeated['album'], '')


if __name__ == '__main__': unittest.main()
