"""Native People writer guarantees across indexes, edits and automatic albums."""
import copy
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image
from photos import auto_albums, live, native_edit, people
from photos.library import Library, LibraryError, digest_file, packed


class NativePeopleTest(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.source=self.root/'source';self.source.mkdir()
        self.library=Library(self.root/'library');self.addCleanup(self.library.db.close)
        self.ids=[];self.originals={}
        for i in range(4):
            path=self.source/f'Photo {i}.png';Image.new('RGB',(24+i,20),(80+i*30,100,170)).save(path)
            self.ids.append(digest_file(path));self.originals[path]=path.read_bytes()
        self.library.import_folders([self.source]);self.library.set_setting('people_enabled',True);self.library.set_setting('people_processing',True)
        self.vector=struct.pack('<128f',1.0,*([0.0]*127))
        self.faces=[dict(rect=[0,0,5000,5000],embedding=self.vector)]

    def snapshot(self):
        return {table:sorted(tuple(row) for row in self.library.db.execute(f'SELECT * FROM {table}'))
                for table in ['photos','people','faces','face_scans','person_tags','person_tag_edits','albums','sources']}

    def scan(self,identity=None,expected=None):
        return people.store(self.library,identity or self.ids[0],self.faces,self.root,expected=expected)

    def person(self):
        return self.library.db.execute('SELECT person_id FROM faces WHERE photo_id=?',(self.ids[0],)).fetchone()[0]

    def test_late_scan_retains_edits_restored_indexes_and_portrait_files(self):
        before=dict(self.library.photo(self.ids[0]));self.library.edit(self.ids[0],'caption','Written while scanning')
        current=self.snapshot();self.assertFalse(self.scan(expected=before));self.assertEqual(self.snapshot(),current)
        self.assertTrue(self.scan());current=self.snapshot()
        self.assertFalse(self.scan());self.assertEqual(self.snapshot(),current)
        for path,data in self.originals.items():
            self.assertEqual(path.read_bytes(),data)
            self.assertEqual(self.library.original(self.library.photo(digest_file(path))).read_bytes(),data)

    def test_paused_disabled_and_private_live_pairs_never_enter_native_selection(self):
        for setting in ['people_processing','people_enabled']:
            self.library.set_setting(setting,False);before=self.snapshot()
            self.assertFalse(self.scan());self.assertEqual(native_edit.people_change(self.library,'next')['saved'],0)
            self.assertEqual(self.snapshot(),before);self.library.set_setting(setting,True)
        movie=self.source/'Motion.mp4';movie.write_bytes(b'Private synthetic motion');self.library.import_folders([self.source])
        motion=digest_file(movie);live.pair(self.library,self.ids[0],motion)
        self.library.db.execute('UPDATE photos SET hidden=1 WHERE id=?',(motion,));self.library.db.commit();before=self.snapshot()
        self.assertFalse(self.scan());self.assertNotEqual(native_edit.people_change(self.library,'next')['id'],self.ids[0])
        self.assertEqual(self.snapshot(),before)

    def test_failure_and_retry_preserve_valid_faces_and_atomic_backup_stamp(self):
        self.assertTrue(self.scan());before=dict(self.library.photo(self.ids[1]))
        self.assertEqual(native_edit.people_change(self.library,'failure',photo=self.ids[1],expected=struct.pack('<d',before['updated']).hex())['saved'],1)
        self.assertEqual(dict(self.library.photo(self.ids[1])),before)
        self.library.db.execute("CREATE TRIGGER stop_retry BEFORE DELETE ON face_scans BEGIN SELECT RAISE(ABORT,'fixture failure'); END");self.library.db.commit()
        unchanged=self.snapshot()
        with self.assertRaises(LibraryError):native_edit.people_change(self.library,'retry')
        self.assertEqual(self.snapshot(),unchanged)
        self.library.db.execute('DROP TRIGGER stop_retry');self.library.db.commit()
        native_edit.people_change(self.library,'retry')
        self.assertEqual(self.library.db.execute('SELECT photo_id FROM face_scans').fetchall()[0][0],self.ids[0])

    def test_tag_and_automatic_membership_write_failure_roll_back_every_table(self):
        self.assertTrue(self.scan());person=self.person();people.edit(self.library,'person_name',person,'Ada')
        self.library.edit_album('','create','Automatic');auto_albums.configure(self.library,'Automatic',[person])
        self.library.db.execute("CREATE TRIGGER stop_auto BEFORE UPDATE OF albums ON photos WHEN NEW.albums<>OLD.albums BEGIN SELECT RAISE(ABORT,'fixture failure'); END");self.library.db.commit()
        before=self.snapshot()
        with self.assertRaises(LibraryError):people.tag(self.library,[self.ids[1]],'Ada')
        self.assertEqual(self.snapshot(),before)
        self.library.db.execute('DROP TRIGGER stop_auto');self.library.db.commit()
        people.tag(self.library,[self.ids[1]],'Ada')
        self.assertIn('Automatic',json.loads(self.library.photo(self.ids[1])['albums']))
        self.library.db.execute("CREATE TRIGGER stop_tag BEFORE INSERT ON person_tags BEGIN SELECT RAISE(ABORT,'fixture failure'); END");self.library.db.commit();before=self.snapshot()
        with self.assertRaises(LibraryError):people.tag(self.library,[self.ids[2]],'New person')
        self.assertEqual(self.snapshot(),before)

    def test_correction_or_merged_rule_failure_keeps_names_faces_and_memberships(self):
        self.assertTrue(self.scan());person=self.person();people.edit(self.library,'person_name',person,'Ada')
        self.library.edit_album('','create','Automatic');auto_albums.configure(self.library,'Automatic',[person])
        other=people.tag(self.library,[self.ids[1]],'Grace')
        self.library.db.execute("CREATE TRIGGER stop_merge BEFORE UPDATE OF auto_people ON albums BEGIN SELECT RAISE(ABORT,'fixture failure'); END");self.library.db.commit();before=self.snapshot()
        with self.assertRaises(LibraryError):people.edit(self.library,'person_name',person,'Grace')
        self.assertEqual(self.snapshot(),before)
        self.library.db.execute('DROP TRIGGER stop_merge');self.library.db.commit()
        self.library.db.execute("CREATE TRIGGER stop_correct BEFORE UPDATE OF excluded ON faces BEGIN SELECT RAISE(ABORT,'fixture failure'); END");self.library.db.commit();before=self.snapshot()
        with self.assertRaises(LibraryError):people.edit(self.library,'person_remove',person,ids=[self.ids[0]])
        self.assertEqual(self.snapshot(),before);self.assertEqual(people.group(self.library,other)['name'],'Grace')

    def test_native_scan_and_rule_failures_never_leave_half_a_scan_or_rule(self):
        self.library.db.execute("CREATE TRIGGER stop_scan BEFORE UPDATE OF dirty ON photos BEGIN SELECT RAISE(ABORT,'fixture failure'); END");self.library.db.commit();before=self.snapshot()
        with self.assertRaises(LibraryError):self.scan()
        self.assertEqual(self.snapshot(),before)
        self.library.db.execute('DROP TRIGGER stop_scan');self.library.db.commit()
        person=people.tag(self.library,[self.ids[0]],'Ada');self.library.edit_album('','create','Automatic')
        self.library.db.execute("CREATE TRIGGER stop_rule BEFORE UPDATE OF albums ON photos WHEN NEW.albums<>OLD.albums BEGIN SELECT RAISE(ABORT,'fixture failure'); END");self.library.db.commit();before=self.snapshot()
        with self.assertRaises(LibraryError):auto_albums.configure(self.library,'Automatic',[person])
        self.assertEqual(self.snapshot(),before)

    def test_native_rule_changes_keep_both_live_components_in_agreement(self):
        movie=self.source/'Motion.mp4';movie.write_bytes(b'Synthetic motion');self.library.import_folders([self.source])
        motion=digest_file(movie);live.pair(self.library,self.ids[0],motion)
        person=people.tag(self.library,[self.ids[0]],'Ada');self.library.edit_album('','create','Automatic')
        auto_albums.configure(self.library,'Automatic',[person])
        for identity in [self.ids[0],motion]:self.assertIn('Automatic',json.loads(self.library.photo(identity)['albums']))
        auto_albums.configure(self.library,'Automatic',[],remove=True)
        for identity in [self.ids[0],motion]:self.assertNotIn('Automatic',json.loads(self.library.photo(identity)['albums']))

    def test_native_helper_loss_and_unfinished_parent_transaction_have_no_write_fallback(self):
        self.library.edit_album('','create','Automatic');before=self.snapshot()
        with patch.object(native_edit,'program',return_value=self.root/'missing'):
            for action in [lambda:people.tag(self.library,[self.ids[0]],'Ada'),lambda:self.scan(),lambda:auto_albums.apply(self.library),lambda:auto_albums.configure(self.library,'Automatic',[])]:
                with self.assertRaises(LibraryError):action()
                self.assertEqual(self.snapshot(),before)
        self.library.db.execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaises(LibraryError):people.tag(self.library,[self.ids[0]],'Ada')
        finally:self.library.db.rollback()
        self.assertEqual(self.snapshot(),before)

    def test_worker_uses_native_selection_publication_and_failure_without_write_fallback(self):
        calls=[];original=native_edit.people_change
        def record(*args,**fields):calls.append(args[1]);return original(*args,**fields)
        count=0
        def infer(*args):
            nonlocal count
            count+=1
            if count==2:raise LibraryError('Synthetic inference failure')
            return copy.deepcopy(self.faces)
        with patch.object(people,'prepare'),patch.object(people,'infer',side_effect=infer),patch.object(native_edit,'people_change',side_effect=record):people._run(str(self.library.root))
        self.assertEqual(calls.count('next'),5);self.assertEqual(calls.count('store'),3);self.assertEqual(calls.count('failure'),1)
        self.assertEqual(self.library.db.execute("SELECT count(*) FROM face_scans WHERE error<>''").fetchone()[0],1)

    def test_interrupted_native_scan_rolls_back_faces_people_and_backup_stamp(self):
        self.library.db.execute("CREATE TRIGGER slow_scan BEFORE UPDATE OF dirty ON photos BEGIN SELECT sum(x) FROM (WITH RECURSIVE work(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM work WHERE x<100000000) SELECT x FROM work); END");self.library.db.commit();before=self.snapshot()
        photo=self.library.photo(self.ids[0])
        request=packed(dict(action='people',operation='store',photo=self.ids[0],expected=struct.pack('<d',photo['updated']).hex(),faces=[dict(rect=[0,0,5000,5000],vector=self.vector.hex())])).encode()+b'\n'
        environment={k:v for k,v in os.environ.items() if k not in {'DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','DBUS_SESSION_BUS_ADDRESS','SESSION_MANAGER'}}
        child=subprocess.Popen([str(native_edit.program()),str(self.library.root/'library.sqlite')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=environment)
        try:
            child.stdin.write(request);child.stdin.flush();time.sleep(.15);self.assertIsNone(child.poll());child.kill();child.wait(timeout=5)
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=5)
            child.stdin.close();child.stdout.close();child.stderr.close()
        self.assertEqual(self.snapshot(),before)
        for message in [b'{"action":"people"',packed(dict(action='people',operation='tag',value='Ada',ids=[self.ids[0],'bad'])).encode()+b'\n']:
            response=subprocess.run([str(native_edit.program()),str(self.library.root/'library.sqlite')],input=message,capture_output=True,timeout=5,env=environment)
            self.assertNotEqual(response.returncode,0);self.assertEqual(self.snapshot(),before)


if __name__=='__main__':unittest.main()
