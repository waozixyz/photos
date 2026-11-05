import copy
import json
import math
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from photos import people
from photos.library import Library, LibraryError, digest_file
from photos.worker import process
from photos.sync import Client
from owner_fixture import ACCOUNT


def vector(first=1.):
    return struct.pack('<128f',first,math.sqrt(1-first*first),*([0.]*126))


class PeopleTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); source = self.root/'source/Trip'; source.mkdir(parents=True)
        self.library = Library(self.root/'library'); self.addCleanup(self.library.db.close)
        self.ids = []
        for i in range(5):
            path = source/f'photo{i}.png'; Image.new('RGB',(31+i,24),(i*30,100,200)).save(path)
            self.ids.append(digest_file(path))
        self.library.import_folders([source.parent]); self.config = dict(root=str(self.library.root))
        self.library.set_setting('people_enabled',True); self.library.set_setting('people_processing',True)
        for i in range(3):
            self.assertTrue(people.store(self.library,self.ids[i],[dict(rect=[1000,1000,5000,6000],embedding=vector(.95-i*.05))],self.root))
        self.person = self.library.db.execute('SELECT person_id FROM faces WHERE photo_id=?',(self.ids[0],)).fetchone()[0]
        self.library.set_setting('people_processing',False)

    def test_native_grouping_name_search_ignore_and_paging_preserve_originals(self):
        originals = {i:self.library.original(self.library.photo(i)).read_bytes() for i in self.ids}
        groups,count = people.catalog(self.library); self.assertEqual(count,1); self.assertEqual(groups[0]['count'],3)
        people.edit(self.library,'person_name',self.person,'Ada')
        self.assertEqual(self.library.query('people',person=self.person,limit=2)['count'],3)
        self.assertEqual(len(self.library.query('people',person=self.person,limit=2,offset=2)['photos']),1)
        self.assertEqual(self.library.query(search='person:Ada')['count'],3)
        self.assertEqual(self.library.query(search='Ada')['count'],3)
        people.edit(self.library,'person_ignore',self.person)
        self.assertFalse(people.catalog(self.library)[0]); self.assertEqual(people.catalog(self.library,mode='ignored')[1],1)
        self.assertEqual(self.library.query(search='person:Ada')['count'],0)
        people.edit(self.library,'person_ignore',self.person)
        self.library.edit(self.ids[1],'hidden_set','1'); self.library.edit(self.ids[2],'trash')
        result = self.library.query('people',person=self.person,hidden_access=True)
        self.assertEqual([p['id'] for p in result['photos']],[self.ids[0]])
        self.assertEqual(people.catalog(self.library)[0][0]['count'],1)
        self.assertEqual(self.library.query(search='person:Ada',hidden_access=True)['count'],1)
        for i,blob in originals.items(): self.assertEqual(self.library.original(self.library.photo(i)).read_bytes(),blob)

    def test_correction_survives_rescan_and_blocks_remerging_wrong_groups(self):
        people.edit(self.library,'person_name',self.person,'Ada')
        people.edit(self.library,'person_remove',self.person,ids=[self.ids[0]])
        face = self.library.db.execute('SELECT * FROM faces WHERE photo_id=?',(self.ids[0],)).fetchone()
        self.assertNotEqual(face['person_id'],self.person); self.assertIn(self.person,json.loads(face['excluded']))
        with self.assertRaises(LibraryError): people.edit(self.library,'person_name',face['person_id'],'Ada')
        self.library.set_setting('people_processing',True)
        self.assertFalse(people.store(self.library,self.ids[0],[dict(rect=[0,0,1000,1000],embedding=vector())],self.root))
        self.assertEqual(self.library.db.execute('SELECT person_id FROM faces WHERE photo_id=?',(self.ids[0],)).fetchone()[0],face['person_id'])
        self.library.edit(self.ids[0],'hidden_set','1')
        with self.assertRaises(LibraryError): people.edit(self.library,'person_remove',face['person_id'],ids=[self.ids[0]])

    def test_distinct_faces_in_one_photo_stay_separate_then_merge_by_name(self):
        self.library.set_setting('people_processing',True)
        people.store(self.library,self.ids[3],[dict(rect=[0,0,3000,3000],embedding=vector()),dict(rect=[5000,0,3000,3000],embedding=vector())],self.root)
        rows = self.library.db.execute('SELECT person_id FROM faces WHERE photo_id=?',(self.ids[3],)).fetchall()
        self.assertEqual(len({r[0] for r in rows}),2)
        self.library.set_setting('people_processing',False)
        people.edit(self.library,'person_name',rows[0][0],'Ada')
        target = people.edit(self.library,'person_name',rows[1][0],'Ada')
        self.assertEqual(target,rows[0][0])
        self.assertEqual(people.catalog(self.library)[1],1)

    def test_hide_or_pause_during_inference_does_not_publish_faces(self):
        self.library.db.execute('DELETE FROM face_scans'); self.library.db.execute('DELETE FROM faces'); self.library.db.commit()
        entered,finish = threading.Event(),threading.Event()
        def infer(library,photo,folder):
            entered.set(); self.assertTrue(finish.wait(5)); return [dict(rect=[0,0,2000,2000],embedding=vector())]
        with patch.object(people,'prepare'),patch.object(people,'infer',side_effect=infer):
            people.enable(self.library,True); self.assertTrue(entered.wait(5))
            self.library.db.execute('UPDATE photos SET hidden=1'); self.library.db.commit()
            people.enable(self.library,False); finish.set()
            with people._mutex: thread = people._jobs.get(str(self.library.root))
            if thread: thread.join(5); self.assertFalse(thread.is_alive())
        self.assertEqual(self.library.db.execute('SELECT count(*) FROM faces').fetchone()[0],0)
        self.assertFalse(list((self.library.root/'private/people').glob('.infer-*')))

    def test_encrypted_backup_and_export_reimport_keep_names_corrections_and_vectors(self):
        people.edit(self.library,'person_name',self.person,'Ada')
        people.edit(self.library,'person_remove',self.person,ids=[self.ids[0]])
        sender = Client(self.library,dict(node='http://127.0.0.1:18080',node_id='a'*64),account=ACCOUNT)
        peer = Library(self.root/'peer'); self.addCleanup(peer.db.close)
        receiver = Client(peer,dict(node='http://127.0.0.1:18080',node_id='a'*64),sender.identity_path,account=ACCOUNT)
        for i in self.ids[:3]:
            record = sender.record(self.library.photo(i), dict(blobs=[]))
            self.assertNotIn('Ada',json.dumps(record)); self.assertNotIn(people.MODEL,json.dumps(record))
            receiver.accept(record); peer.db.commit()
            self.assertEqual(people.export_index(peer,i),people.export_index(self.library,i))
        self.assertFalse(peer.setting('people_enabled',False))
        exported = self.root/'export'
        self.library.export(self.ids[0],exported)
        restored = Library(self.root/'reimport'); self.addCleanup(restored.db.close); restored.import_folders([exported])
        self.assertEqual(people.export_index(restored,self.ids[0]),people.export_index(self.library,self.ids[0]))
        self.assertNotIn('photosPeople',json.loads(restored.photo(self.ids[0])['metadata']))
        self.assertEqual(restored.original(restored.photo(self.ids[0])).read_bytes(),self.library.original(self.library.photo(self.ids[0])).read_bytes())

    def test_invalid_private_records_are_atomic_and_cannot_move_face_identity(self):
        before = people.export_index(self.library,self.ids[0])
        for mutate in [lambda v:v.update(model='unknown'),lambda v:v['faces'][0].update(rect=[0,0,10001,100]),
                       lambda v:v['faces'][0].update(vector='x'*684),lambda v:v['faces'][0]['person'].update(ignored=True),
                       lambda v:v['faces'][0]['person'].update(updated=float('nan')),
                       lambda v:v['faces'].append(copy.deepcopy(v['faces'][0]))]:
            value = copy.deepcopy(before); mutate(value)
            with self.assertRaises(ValueError): people.accept_index(self.library,self.ids[0],value)
            self.assertEqual(people.export_index(self.library,self.ids[0]),before)
        with self.assertRaises(ValueError): people.accept_index(self.library,self.ids[4],before)
        self.assertEqual(people.export_index(self.library,self.ids[0]),before)

    def test_real_native_models_record_empty_scan_without_changing_synthetic_photo(self):
        try: people.prepare()
        except LibraryError: self.skipTest('Install optional verified face models to test real inference')
        photo = self.library.photo(self.ids[4]); original = self.library.original(photo).read_bytes()
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            self.assertEqual(people.infer(self.library,photo,Path(temporary)),[])
        self.assertEqual(self.library.original(photo).read_bytes(),original)
        for blob in [b'',b'bad',struct.pack('<128f',float('nan'),*([0.]*127)),struct.pack('<128f',*([1.]*128))]:
            self.assertFalse(people.valid_vector(blob))

    def test_face_rich_sync_batches_fit_node_limit_and_failure_keeps_edits_queued(self):
        client = Client(self.library,dict(node='http://127.0.0.1:18080',node_id='a'*64),account=ACCOUNT)
        self.library.db.execute('UPDATE albums SET dirty=0'); self.library.db.commit()
        batches = []
        def exchange(records=None):
            if records:
                batches.append(records)
                self.assertLess(len(json.dumps(records).encode()),800000)
                if len(batches)==2: raise ValueError('Interrupted second page')
            return dict(status='ok')
        with patch.object(client,'check_node'),patch.object(client,'exchange',side_effect=exchange),\
             patch.object(client,'prepare_entity',return_value=dict(blobs=[])),\
             patch.object(client,'records',side_effect=lambda photo,entity:[dict(id=photo['id'],ciphertext='x'*260000)]):
            with self.assertRaises(ValueError): client.sync()
        self.assertEqual(len(batches),2)
        self.assertEqual(self.library.db.execute('SELECT count(*) FROM photos WHERE dirty=1').fetchone()[0],5)

    def test_catalog_is_paged_and_hiding_its_last_photo_revokes_name_and_thumbnail(self):
        source = self.root/'many'; source.mkdir()
        for i in range(65): Image.new('RGB',(40+i,20),(i,30,100)).save(source/f'image{i}.png')
        self.library.import_folders([source]); self.library.set_setting('people_processing',True)
        # Orthogonal identities avoid needing actual model inference in this
        # paging check; the native matcher still validates every vector.
        for i,path in enumerate(sorted(source.iterdir())):
            blob = struct.pack('<128f',*([0.]*i+[1.]+[0.]*(127-i)))
            people.store(self.library,digest_file(path),[dict(rect=[0,0,5000,5000],embedding=blob)],self.root)
        self.library.set_setting('people_processing',False)
        first,count = people.catalog(self.library); following,total = people.catalog(self.library,offset=60)
        self.assertEqual(len(first),60); self.assertGreater(count,60); self.assertEqual(total,count)
        self.assertFalse({g['id'] for g in first}&{g['id'] for g in following})
        people.edit(self.library,'person_name',self.person,'Ada')
        for row in self.library.db.execute('SELECT photo_id FROM faces WHERE person_id=?',(self.person,)).fetchall(): self.library.edit(row[0],'hidden_set','1')
        result = process(dict(action='query',view='people',person=self.person),self.config)
        self.assertFalse(result['photos']); self.assertEqual(result['person_detail']['name'],'')
        with self.assertRaises(LibraryError): people.edit(self.library,'person_name',self.person,'New name')
        self.library.set_setting('people_enabled',False)
        self.assertEqual(self.library.query(search='person:Ada')['count'],0)
