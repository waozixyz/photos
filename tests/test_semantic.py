import json
import math
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from photos import semantic
from photos.library import Library, LibraryError, digest_file
from photos.worker import process
from photos.sync import Client


def vector(first=1.0):
    return struct.pack('<512f', first, math.sqrt(1-first*first), *([0.0]*510))


class SemanticTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name); source = self.root / 'source/Colors'; source.mkdir(parents=True)
        self.library = Library(self.root / 'library'); self.addCleanup(self.library.db.close)
        self.ids = []
        for i in range(7):
            path = source / f'image{i}.png'; Image.new('RGB', (30+i, 20), (230-i*20, i*30, 10)).save(path)
            self.ids.append(digest_file(path))
        self.library.import_folders([source.parent])
        for i, photo_id in enumerate(self.ids):
            self.library.edit(photo_id, 'taken', f'2024-06-{10+i:02d}T00:00:00Z')
        self.library.edit(self.ids[3], 'hidden_set', '1'); self.library.edit(self.ids[4], 'trash')
        self.library.edit(self.ids[1], 'archived_set', '1')
        self.library.set_setting('semantic_enabled', True)
        self.library.set_setting('semantic_processing', False)
        for i, score in enumerate([.9,.8,.1,1.,1.,.7,.6]):
            self.library.db.execute('INSERT INTO semantic_embeddings VALUES(?,?,?,?)', (self.ids[i], semantic.MODEL, vector(score), ''))
        self.library.db.commit(); self.config = dict(root=str(self.library.root))
        self.blob = vector()

    def search(self, **request):
        with patch.object(semantic, 'infer', return_value=self.blob), patch.object(semantic, 'text_tensor', return_value=b'query'):
            return self.library.query('semantic', search='a red sunset', **request)

    def test_native_rank_paging_privacy_and_original_preservation(self):
        originals = {p: self.library.original(self.library.photo(p)).read_bytes() for p in self.ids}
        result = self.search(limit=2)
        self.assertEqual(result['count'],4)
        self.assertEqual([p['id'] for p in result['photos']],self.ids[:2])
        self.assertEqual([p['id'] for p in self.search(limit=2,offset=2)['photos']],self.ids[5:])
        self.assertEqual([p['id'] for p in self.search(sort='oldest')['photos']],self.ids[:2]+self.ids[5:])
        self.assertEqual([p['id'] for p in self.search(hidden_access=True)['photos']],self.ids[:2]+self.ids[5:])
        self.assertFalse(self.library.query('semantic')['photos'])
        for p, content in originals.items():
            self.assertEqual(self.library.original(self.library.photo(p)).read_bytes(),content)
        self.assertFalse(list((self.library.root/'private/semantic').glob('.query-*')))

    def test_corrupt_vectors_and_missing_models_fail_closed(self):
        for bad in [b'short',struct.pack('<512f',float('nan'),*([0.0]*511)),struct.pack('<512f',*([1.0]*512))]:
            self.library.db.execute('UPDATE semantic_embeddings SET embedding=? WHERE photo_id=?',(bad,self.ids[0]));self.library.db.commit()
            self.assertEqual(self.search()['count'],3)
        self.library.set_setting('semantic_enabled',False)
        with patch.object(semantic,'infer',side_effect=AssertionError('Disabled inference must not run')):
            self.assertFalse(self.library.query('semantic',search='anything')['photos'])
        with patch.object(semantic,'prepare',side_effect=LibraryError('Local models missing')):
            with self.assertRaises(LibraryError):process(dict(action='semantic_enable'),self.config)
        self.assertFalse(semantic.state(self.library)['semantic_enabled'])

    def test_hide_and_pause_during_inference_publish_no_new_index(self):
        self.library.db.execute('DELETE FROM semantic_embeddings');self.library.db.commit()
        entered,finish=threading.Event(),threading.Event()
        def infer(*args):entered.set();self.assertTrue(finish.wait(5));return self.blob
        with patch.object(semantic,'prepare'),patch.object(semantic,'image_tensor',return_value=b'image'),patch.object(semantic,'infer',side_effect=infer):
            semantic.enable(self.library,True);self.assertTrue(entered.wait(5))
            # The newest candidate is hidden while its model is processing.
            self.library.edit(self.ids[6],'hidden_set','1')
            semantic.enable(self.library,False);finish.set()
            with semantic._mutex:thread=semantic._jobs.get(str(self.library.root))
            if thread:thread.join(5);self.assertFalse(thread.is_alive())
        self.assertEqual(self.library.db.execute('SELECT COUNT(*) FROM semantic_embeddings').fetchone()[0],0)
        self.assertTrue(semantic.state(self.library)['semantic_enabled'])
        self.assertFalse(semantic.state(self.library)['semantic_processing'])

    def test_hiding_between_native_rank_and_hydration_exposes_no_paths(self):
        from photos import query
        original=query.page
        def hide(library,**request):
            response=original(library,**request)
            peer=Library(library.root)
            try:peer.edit(self.ids[0],'hidden_set','1')
            finally:peer.db.close()
            return response
        with patch.object(query,'page',side_effect=hide):
            self.assertNotIn(self.ids[0],{p['id'] for p in self.search()['photos']})

    def test_embeddings_sync_only_inside_authenticated_encryption(self):
        sender=Client(self.library,dict(node='http://127.0.0.1:18080',node_id='a'*64))
        peer=Library(self.root/'peer');self.addCleanup(peer.db.close)
        receiver=Client(peer,dict(node='http://127.0.0.1:18080',node_id='a'*64),sender.identity_path)
        record=sender.record(self.library.photo(self.ids[0]),manifest={})
        self.assertNotIn(semantic.MODEL,json.dumps(record));self.assertNotIn('values',json.dumps(record))
        receiver.accept(record);peer.db.commit()
        self.assertEqual(semantic.encrypted_index(peer,self.ids[0]),semantic.encrypted_index(self.library,self.ids[0]))
        self.assertFalse(peer.setting('semantic_enabled',False))
        # Authentication binds the index to its photo just like other metadata.
        with self.assertRaises(ValueError):receiver.accept({**record,'id':self.ids[1]})
        for value in [[],{},dict(model=semantic.MODEL,values=[True]*512),dict(model=semantic.MODEL,values=[float('nan')]*512)]:
            with self.assertRaises(ValueError):semantic.accept_index(peer,self.ids[0],value)
        self.assertIsNotNone(semantic.encrypted_index(peer,self.ids[0]))

    def test_compact_real_embeddings_fit_normal_sync_batches(self):
        import random
        from photos.library import packed
        generator = random.Random(1)
        values = [generator.uniform(-1,1) for _ in range(512)]
        norm = math.sqrt(sum(v*v for v in values))
        blob = struct.pack('<512f', *(v/norm for v in values))
        self.library.db.execute('UPDATE semantic_embeddings SET embedding=? WHERE photo_id=?', (blob,self.ids[0]))
        self.library.db.commit()
        sender = Client(self.library,dict(node='http://127.0.0.1:18080',node_id='a'*64))
        record = sender.record(self.library.photo(self.ids[0]),manifest={})
        self.assertLess(len(packed(dict(records=[record]*50)).encode()), 900000)
        restored = Library(self.root/'compact'); self.addCleanup(restored.db.close)
        semantic.accept_index(restored,self.ids[0],semantic.encrypted_index(self.library,self.ids[0]))
        self.assertEqual(restored.db.execute('SELECT embedding FROM semantic_embeddings').fetchone()[0],blob)

    def test_real_native_model_color_search_and_text_bounds(self):
        try:semantic.prepare()
        except LibraryError:self.skipTest('Local models are optional; run make semantic-models to verify actual inference')
        red=Image.new('RGB',(300,200),(255,0,0));blue=Image.new('RGB',(300,200),(0,0,255))
        source=self.root/'real';source.mkdir();red.save(source/'red.png');blue.save(source/'blue.png')
        red.save(source/'red-twin.png',compress_level=0);self.library.import_folders([source])
        from photos import native_edit
        self.library.set_setting('semantic_processing',True)
        for path in [source/'red.png',source/'blue.png',source/'red-twin.png']:
            photo=self.library.photo(digest_file(path));blob=semantic.infer(self.library,'image',semantic.image_tensor(self.library,photo))
            self.assertTrue(native_edit.semantic_index(self.library,'publish',dict(photo),blob))
        self.library.set_setting('semantic_processing',False)
        text=semantic.infer(self.library,'text',semantic.text_tensor('a completely red image'))
        values=struct.unpack('<512f',text)
        def dot(path):return sum(a*b for a,b in zip(values,struct.unpack('<512f',self.library.db.execute('SELECT embedding FROM semantic_embeddings WHERE photo_id=?',(digest_file(path),)).fetchone()[0])))
        self.assertGreater(dot(source/'red.png'),dot(source/'blue.png'))
        similar = self.library.query('similar',similar_id=digest_file(source/'red.png'))
        self.assertEqual(similar['photos'][0]['id'],digest_file(source/'red-twin.png'))
        self.assertNotIn(digest_file(source/'red.png'),[p['id'] for p in similar['photos']])
        self.assertEqual(len(semantic.text_tensor('cat '*120)),616)
        for text in ['',[],True,'a\0b','a'*513]:
            with self.assertRaises(LibraryError):semantic.text_tensor(text)
        self.assertFalse(list((self.library.root/'private/semantic').glob('.infer-*')))
