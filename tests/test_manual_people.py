import copy
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from photos import auto_albums, people, live
from photos.library import Library, LibraryError, digest_file
from photos.sync import Client
from owner_fixture import ACCOUNT
from photos.worker import process
from test_people import vector


class ManualPeopleTest(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.source=self.root/'source/Family';self.source.mkdir(parents=True)
        self.ids=[];self.originals={}
        for i in range(10):
            path=self.source/f'Photo {i}.png';Image.new('RGB',(90+i,80),(i*20,50,130)).save(path)
            self.ids.append(digest_file(path));self.originals[path]=path.read_bytes()
        self.library=Library(self.root/'library');self.addCleanup(self.library.db.close)
        self.library.import_folders([self.source.parent]);self.config=dict(root=str(self.library.root))
        self.library.set_setting('people_enabled',True)
        for i,photo_id in enumerate(self.ids):
            self.library.edit(photo_id,'taken',f'{2020+i//5}-06-{1+i%5:02d}T12:00:00Z')

    def test_manual_tags_find_unrecognized_items_and_do_not_complete_scans(self):
        person=people.tag(self.library,self.ids[:3],' Ada ')
        self.assertEqual(people.tag(self.library,[self.ids[0]],'ada'),person)
        self.assertEqual(self.library.query(search='person:Ada')['count'],3)
        self.assertEqual(self.library.query(search='Ada')['count'],3)
        self.assertEqual(self.library.query('people',person=person)['count'],3)
        self.assertEqual(people.catalog(self.library)[0][0]['count'],3)
        self.assertEqual(self.library.db.execute('SELECT count(*) FROM face_scans').fetchone()[0],0)
        self.assertEqual(self.library.db.execute('SELECT count(*) FROM faces').fetchone()[0],0)
        self.assertEqual(self.library.details(self.ids[0])['people'],'People: Ada')
        # Face processing later still runs and does not erase explicit assignments.
        self.library.set_setting('people_processing',True)
        self.assertTrue(people.store(self.library,self.ids[0],[],self.root))
        self.assertEqual(self.library.query(search='person:Ada')['count'],3)
        self.library.set_setting('people_processing',False)
        for path,original in self.originals.items():self.assertEqual(path.read_bytes(),original)
        self.library.import_folders([self.source.parent])
        self.assertEqual(self.library.query(search='person:Ada')['count'],3)

    def test_detected_faces_and_manual_memberships_are_distinct_and_deduplicated(self):
        self.library.set_setting('people_processing',True)
        people.store(self.library,self.ids[0],[dict(rect=[0,0,5000,5000],embedding=vector())],self.root)
        self.library.set_setting('people_processing',False)
        person=self.library.db.execute('SELECT person_id FROM faces WHERE photo_id=?',(self.ids[0],)).fetchone()[0]
        people.edit(self.library,'person_name',person,'Ada')
        self.assertEqual(people.tag(self.library,self.ids[:2],'Ada'),person)
        self.assertEqual(self.library.query(search='person:Ada')['count'],2)
        self.assertEqual(people.catalog(self.library)[0][0]['count'],2)
        people.tag(self.library,[self.ids[0]],'Ada',remove=True)
        self.assertEqual(self.library.query(search='person:Ada')['count'],2) # Detected face retained.
        people.edit(self.library,'person_remove',person,ids=[self.ids[0],self.ids[1]])
        self.assertEqual(self.library.query(search='person:Ada')['count'],0)
        corrected=self.library.db.execute('SELECT * FROM faces WHERE photo_id=?',(self.ids[0],)).fetchone()
        self.assertIn(person,json.loads(corrected['excluded']))
        # An explicit manual tag can override a rejected suggestion without undoing it.
        people.tag(self.library,[self.ids[0]],'Ada')
        self.assertEqual(self.library.query(search='person:Ada')['count'],1)
        self.assertEqual(self.library.db.execute('SELECT excluded FROM faces WHERE id=?',(corrected['id'],)).fetchone()[0],corrected['excluded'])

    def test_hidden_trash_live_components_and_mixed_selections_are_atomic(self):
        self.library.edit(self.ids[1],'hidden_set','1')
        for ids in [[self.ids[1]],[self.ids[0],self.ids[1]]]:
            with self.assertRaises(LibraryError):people.tag(self.library,ids,'Private')
        self.assertEqual(self.library.db.execute('SELECT count(*) FROM people').fetchone()[0],0)
        self.assertEqual(self.library.db.execute('SELECT count(*) FROM person_tags').fetchone()[0],0)
        person=people.tag(self.library,[self.ids[0]],'Ada')
        self.library.edit(self.ids[0],'trash')
        with self.assertRaises(LibraryError):people.tag(self.library,[self.ids[0]],'Ada',remove=True)
        self.assertFalse(people.catalog(self.library)[0]);self.assertEqual(self.library.details(self.ids[0])['people'],'')
        self.library.edit(self.ids[0],'restore')
        motion=self.source/'Motion.mp4';motion.write_bytes(b'Synthetic motion')
        self.library.import_folders([self.source.parent]);motion_id=digest_file(motion)
        live.pair(self.library,self.ids[0],motion_id)
        with self.assertRaises(LibraryError):people.tag(self.library,[motion_id],'Ada')
        self.library.db.execute('UPDATE photos SET hidden=1 WHERE id=?',(motion_id,));self.library.db.commit()
        self.assertEqual(self.library.query(search='person:Ada')['count'],0)
        self.assertFalse(people.catalog(self.library)[0])
        with self.assertRaises(LibraryError):people.tag(self.library,[self.ids[0]],'Ada')

    def test_rename_merge_ignore_and_disable_keep_manual_identity(self):
        person=people.tag(self.library,self.ids[:3],'Ada')
        second=people.tag(self.library,self.ids[2:5],'Grace')
        people.edit(self.library,'person_name',second,'Ada')
        self.assertEqual(self.library.query(search='person:Ada')['count'],5)
        self.assertEqual(self.library.db.execute('SELECT count(*) FROM person_tags WHERE person_id=?',(second,)).fetchone()[0],0)
        people.edit(self.library,'person_name',person,'New name')
        self.assertEqual(self.library.query(search='person:"New name"')['count'],5)
        people.edit(self.library,'person_ignore',person)
        self.assertEqual(self.library.query(search='person:"New name"')['count'],0)
        self.assertEqual(people.catalog(self.library,mode='ignored')[0][0]['count'],5)
        people.edit(self.library,'person_ignore',person)
        self.library.set_setting('people_enabled',False)
        self.assertEqual(self.library.query(search='person:"New name"')['count'],0)
        with self.assertRaises(LibraryError):people.tag(self.library,[self.ids[5]],'New name')
        self.library.set_setting('people_enabled',True)
        self.assertEqual(self.library.query(search='person:"New name"')['count'],5)

    def test_memory_lane_and_auto_people_albums_include_manual_stills(self):
        person=people.tag(self.library,self.ids,'Ada')
        self.assertEqual(self.library.query('lane',person=person)['lane_years'],[2020,2021])
        self.library.edit_album('','create','Auto Ada')
        auto_albums.configure(self.library,'Auto Ada',[person])
        self.assertEqual(self.library.query(album='Auto Ada')['count'],10)
        # Removing a manual identity does not silently delete an earlier album membership.
        people.tag(self.library,[self.ids[0]],'Ada',remove=True)
        self.assertEqual(self.library.query(search='person:Ada')['count'],9)
        self.assertEqual(self.library.query(album='Auto Ada')['count'],10)
        auto_albums.configure(self.library,'Auto Ada',[],remove=True)
        self.assertEqual(self.library.query(album='Auto Ada')['count'],0)

    def test_manual_video_tagging_is_searchable_but_not_memory_lane_eligibility(self):
        movie=self.source/'Video.mp4';movie.write_bytes(b'Synthetic standalone movie')
        self.library.import_folders([self.source.parent]);video_id=digest_file(movie)
        person=people.tag(self.library,[video_id],'Ada')
        self.assertEqual(self.library.query(search='type:video person:Ada')['count'],1)
        self.assertEqual(self.library.query('people',person=person)['count'],1)
        self.assertEqual(people.catalog(self.library)[0][0]['count'],1)
        self.assertEqual(self.library.query('lane',person=person)['count'],0)

    def test_encrypted_export_restore_untag_and_legacy_records_preserve_scans(self):
        person=people.tag(self.library,self.ids[:2],'Ada')
        sender=Client(self.library,dict(node='http://127.0.0.1:18080',node_id='a'*64),account=ACCOUNT)
        peer=Library(self.root/'peer');self.addCleanup(peer.db.close)
        receiver=Client(peer,dict(node='http://127.0.0.1:18080',node_id='a'*64),sender.identity_path,account=ACCOUNT)
        peer.set_setting('people_enabled',True)
        for photo_id in self.ids[:2]:
            record=sender.record(self.library.photo(photo_id), dict(blobs=[]))
            self.assertNotIn('Ada',json.dumps(record));self.assertNotIn('tags',json.dumps(record))
            receiver.accept(record);peer.db.commit()
            self.assertEqual(people.export_index(peer,photo_id),people.export_index(self.library,photo_id))
            self.assertFalse(peer.db.execute('SELECT 1 FROM face_scans WHERE photo_id=?',(photo_id,)).fetchone())
        self.assertEqual(peer.query(search='person:Ada')['count'],2)
        target=self.root/'export';self.library.export(self.ids[0],target)
        imported=Library(self.root/'imported');self.addCleanup(imported.db.close);imported.import_folders([target])
        imported.set_setting('people_enabled',True)
        self.assertEqual(imported.query(search='person:Ada')['count'],1)
        self.assertFalse(imported.db.execute('SELECT 1 FROM face_scans').fetchone())
        people.tag(self.library,[self.ids[0]],'Ada',remove=True)
        record=sender.record(self.library.photo(self.ids[0]), dict(blobs=[]));receiver.accept(record);peer.db.commit()
        self.assertEqual(peer.query(search='person:Ada')['count'],1)
        self.assertEqual(people.export_index(peer,self.ids[0])['tags'],[])
        # Tags from an unscanned peer must not remove another device's completed faces.
        peer.set_setting('people_processing',True)
        people.store(peer,self.ids[0],[dict(rect=[0,0,5000,5000],embedding=vector())],self.root)
        peer.set_setting('people_processing',False)
        self.assertTrue(peer.db.execute('SELECT 1 FROM faces WHERE photo_id=?',(self.ids[0],)).fetchone())
        people.accept_index(peer,self.ids[0],people.export_index(self.library,self.ids[0]));peer.db.commit()
        self.assertTrue(peer.db.execute('SELECT 1 FROM faces WHERE photo_id=?',(self.ids[0],)).fetchone())
        legacy=copy.deepcopy(people.export_index(peer,self.ids[0]));legacy.pop('tags');legacy.pop('scanned')
        people.tag(peer,[self.ids[0]],'Ada')
        people.accept_index(peer,self.ids[0],legacy);peer.db.commit()
        self.assertTrue(peer.db.execute('SELECT 1 FROM person_tags WHERE photo_id=?',(self.ids[0],)).fetchone())

    def test_invalid_tag_batches_and_private_indexes_fail_before_mutation(self):
        for ids,name in [([], 'Ada'),(self.ids*101,'Ada'),(['bad'],'Ada'),(self.ids[:1],''),(self.ids[:1],'a'*513),(self.ids[:1],'bad\0name')]:
            with self.assertRaises(LibraryError):people.tag(self.library,ids,name)
        self.assertEqual(self.library.db.execute('SELECT count(*) FROM people').fetchone()[0],0)
        people.tag(self.library,[self.ids[0]],'Ada');index=people.export_index(self.library,self.ids[0])
        before=copy.deepcopy(index)
        for changes in [dict(tags='bad'),dict(tags=index['tags']*65),dict(tags=index['tags']*2),dict(scanned=1),dict(scanned=False,faces=[{}])]:
            with self.assertRaises(ValueError):people.accept_index(self.library,self.ids[0],{**index,**changes})
            self.assertEqual(people.export_index(self.library,self.ids[0]),before)
        self.library.import_folders([self.source.parent])
        self.assertEqual(self.library.query(search='person:Ada')['count'],1)

    def test_single_and_selection_rpc_returns_details_and_keeps_private_items_atomic(self):
        tagged=process(dict(action='person_tag',id=self.ids[0],value='Ada'),self.config)
        self.assertEqual(tagged['detail']['people'],'People: Ada')
        process(dict(action='person_tag',ids=self.ids[1:3],value='Ada'),self.config)
        self.assertEqual(self.library.query(search='person:Ada')['count'],3)
        self.library.edit(self.ids[3],'hidden_set','1')
        with self.assertRaises(LibraryError):
            process(dict(action='person_tag',ids=self.ids[2:5],value='Grace'),self.config)
        self.assertEqual(self.library.query(search='person:Grace')['count'],0)
        removed=process(dict(action='person_untag',id=self.ids[0],value='Ada'),self.config)
        self.assertEqual(removed['detail']['people'],'')
        self.assertEqual(self.library.query(search='person:Ada')['count'],2)


if __name__=='__main__':unittest.main()
