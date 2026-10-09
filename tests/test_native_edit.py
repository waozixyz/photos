import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from PIL import Image
from photos import native_edit
from photos.library import Library, LibraryError, digest_file, packed


class NativeEditTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.source = self.root/'source'; self.source.mkdir()
        self.original = self.source/'Photo.png'; Image.new('RGB',(16,16),'blue').save(self.original)
        self.library = Library(self.root/'library'); self.addCleanup(self.library.db.close)
        self.library.import_folders([self.source]); self.photo_id = digest_file(self.original)
        self.original_bytes = self.original.read_bytes()

    def child(self, action='favorite_set', value='1', ids=None):
        process = subprocess.Popen([str(native_edit.program()),str(self.library.root/'library.sqlite')],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            env={k:v for k,v in os.environ.items() if k not in {'DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','DBUS_SESSION_BUS_ADDRESS'}})
        def close():
            if process.poll() is None: process.kill()
            process.wait(timeout=3)
            process.stdin.close(); process.stdout.close(); process.stderr.close()
        self.addCleanup(close)
        process.stdin.write(packed(dict(action=action,value=value,ids=ids or [self.photo_id])).encode()+b'\n')
        process.stdin.flush()
        return process

    def test_native_writer_waits_for_authorization_and_interruption_rolls_back(self):
        child = self.child()
        self.assertEqual(json.loads(child.stdout.readline())['authorize'],[self.photo_id])
        self.assertEqual(self.library.photo(self.photo_id)['favorite'],0)
        child.kill(); child.wait(timeout=3)
        self.assertEqual(self.library.photo(self.photo_id)['favorite'],0)
        self.library.edit(self.photo_id,'favorite_set','1')
        self.assertEqual(self.library.photo(self.photo_id)['favorite'],1)
        self.assertEqual(self.original.read_bytes(),self.original_bytes)

    def test_native_expired_hidden_lease_and_denied_component_preserve_the_whole_batch(self):
        self.library.edit(self.photo_id,'hidden_set','1')
        def authorize(photo): return True
        authorize.deadline = lambda:time.monotonic()-1
        with self.assertRaisesRegex(LibraryError,'Hidden'):
            self.library.edit(self.photo_id,'favorite_set','1',authorize=authorize)
        self.assertEqual(self.library.photo(self.photo_id)['favorite'],0)
        self.assertEqual(self.original.read_bytes(),self.original_bytes)

    def test_hidden_lease_expires_during_sql_and_transaction_rolls_back(self):
        self.library.edit(self.photo_id,'hidden_set','1')
        # A slow SQLite trigger makes expiry happen during an actual update,
        # after authorization. No wall-clock patch or native test bypass.
        self.library.db.execute("""CREATE TRIGGER slow_edit AFTER UPDATE OF favorite ON photos BEGIN
            SELECT sum(x) FROM (WITH RECURSIVE work(x) AS
                (SELECT 1 UNION ALL SELECT x+1 FROM work WHERE x<1000000) SELECT x FROM work);
            END""")
        self.library.db.commit()
        child = self.child()
        self.assertIn(self.photo_id,json.loads(child.stdout.readline())['authorize'])
        child.stdin.write(packed(dict(allow=1,until=time.monotonic()+.05)).encode()+b'\n')
        child.stdin.flush()
        self.assertIn('expired',json.loads(child.stdout.readline())['error'])
        self.assertEqual(child.wait(timeout=4),1)
        self.assertEqual(self.library.photo(self.photo_id)['favorite'],0)

    def test_native_text_keeps_unicode_nul_full_limit_and_original_bytes(self):
        value='Captión 日本語\n\0'+('é'*100)
        self.library.edit(self.photo_id,'caption',value)
        self.assertEqual(self.library.photo(self.photo_id)['caption'],value)
        self.library.edit(self.photo_id,'tags','x'*8192)
        self.assertEqual(self.library.photo(self.photo_id)['tags'],'x'*8192)
        before = self.library.photo(self.photo_id)
        for action,value in [('tags','x'*8193),('caption',None),('favorite_set','true')]:
            with self.assertRaises(LibraryError):self.library.edit(self.photo_id,action,value)
            self.assertEqual(self.library.photo(self.photo_id),before)
        self.assertEqual(self.library.original(self.library.photo(self.photo_id)).read_bytes(),self.original_bytes)

    def test_duplicate_ids_change_once_large_batch_is_atomic_and_update_stamp_advances(self):
        self.library.edit_many([self.photo_id]*1000,'favorite')
        self.assertEqual(self.library.photo(self.photo_id)['favorite'],1)
        columns = list(self.library.photo(self.photo_id))
        values = self.library.photo(self.photo_id)
        ids = [self.photo_id]
        for i in range(1,1000):
            photo_id=hashlib.sha256(str(i).encode()).hexdigest(); row={**values,'id':photo_id}
            self.library.db.execute('INSERT INTO photos('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',[row[key] for key in columns])
            ids.append(photo_id)
        self.library.db.execute('UPDATE photos SET updated=2000000000.0,dirty=0'); self.library.db.commit()
        with self.assertRaises(LibraryError):self.library.edit_many([*ids[:999],'f'*64],'archived_set','1')
        self.assertEqual(self.library.db.execute('SELECT sum(archived),sum(dirty) FROM photos').fetchone()[:],(0,0))
        self.library.edit_many(ids,'archived_set','1')
        self.assertEqual(self.library.db.execute('SELECT sum(archived),sum(dirty) FROM photos').fetchone()[:],(1000,1000))
        self.assertGreater(self.library.photo(self.photo_id)['updated'],2000000000.0)

    def test_pending_python_transaction_is_retained_and_invalid_native_requests_change_nothing(self):
        self.library.db.execute('UPDATE photos SET tags=? WHERE id=?',('Pending',self.photo_id))
        with self.assertRaisesRegex(LibraryError,'current library operation'):
            self.library.edit(self.photo_id,'favorite')
        self.assertTrue(self.library.db.in_transaction)
        self.library.db.rollback()
        for action,value,ids in [('caption',2,None),('favorite_set','bad',None),('favorite',None,['../outside'])]:
            child=self.child(action,value,ids)
            self.assertIn('error',json.loads(child.stdout.readline()))
            self.assertEqual(child.wait(timeout=3),1)
        self.assertEqual(self.library.photo(self.photo_id)['favorite'],0)
        self.assertEqual(self.library.photo(self.photo_id)['tags'],'')

    def test_native_album_names_ids_provenance_and_manual_removals_survive_rescan(self):
        name="O'Brien / 旅行 %_"
        self.library.edit(self.photo_id,'album_add',name)
        album=self.library.album_details(name)
        self.assertEqual(album['id'],hashlib.sha256(('photos.album.v1:'+name).encode()).hexdigest())
        other='b'*64
        edits=dict(taken=True,removed_albums=[name,'Old'],auto_albums={album['id']:['a'*64],other:['c'*64]})
        self.library.db.execute('UPDATE photos SET edits=? WHERE id=?',(packed(edits),self.photo_id));self.library.db.commit()
        self.library.edit_many([self.photo_id,self.photo_id],'album_add',name)
        saved=json.loads(self.library.photo(self.photo_id)['edits'])
        self.assertEqual(saved,dict(taken=True,removed_albums=['Old'],auto_albums={other:['c'*64]}))
        self.library.edit(self.photo_id,'album_remove',name)
        self.assertNotIn(name,json.loads(self.library.photo(self.photo_id)['albums']))
        self.assertIn(name,json.loads(self.library.photo(self.photo_id)['edits'])['removed_albums'])
        self.library.import_folders([self.source])
        self.assertNotIn(name,json.loads(self.library.photo(self.photo_id)['albums']))
        self.assertEqual(self.original.read_bytes(),self.original_bytes)

    def test_native_album_reused_name_retains_renamed_identity_and_smart_rules(self):
        self.library.edit(self.photo_id,'album_add','Name')
        first=self.library.album_details('Name')['id']
        self.library.rename_album('Name','Renamed')
        self.library.edit(self.photo_id,'album_add','Name')
        self.assertNotEqual(self.library.album_details('Name')['id'],first)
        self.assertEqual(self.library.album_details('Renamed')['id'],first)
        self.assertEqual(self.library.album_details('Name')['id'],hashlib.sha256((first+':next').encode()).hexdigest())
        self.library.edit_album('','smart_create','Smart',search='type:photo')
        before=self.library.photo(self.photo_id)
        for action in ['album_add','album_remove']:
            with self.assertRaisesRegex(LibraryError,'Smart album'):
                self.library.edit(self.photo_id,action,'Smart')
            self.assertEqual(self.library.photo(self.photo_id),before)

    def test_native_failed_selection_and_expired_lease_roll_back_new_album_definition(self):
        before=self.library.photo(self.photo_id)
        with self.assertRaises(LibraryError):
            self.library.edit_many([self.photo_id,'f'*64],'album_add','Never created')
        self.assertEqual(self.library.db.execute('SELECT count(*) FROM albums WHERE name=?',('Never created',)).fetchone()[0],0)
        self.assertEqual(self.library.photo(self.photo_id),before)
        self.library.edit(self.photo_id,'hidden_set','1')
        self.library.db.execute("""CREATE TRIGGER slow_album AFTER INSERT ON albums BEGIN
            SELECT sum(x) FROM (WITH RECURSIVE work(x) AS
                (SELECT 1 UNION ALL SELECT x+1 FROM work WHERE x<1000000) SELECT x FROM work);
            END""")
        self.library.db.commit()
        child=self.child('album_add','Expired album')
        self.assertIn(self.photo_id,json.loads(child.stdout.readline())['authorize'])
        child.stdin.write(packed(dict(allow=1,until=time.monotonic()+.05)).encode()+b'\n');child.stdin.flush()
        self.assertIn('expired',json.loads(child.stdout.readline())['error'])
        self.assertEqual(child.wait(timeout=4),1)
        self.assertEqual(self.library.db.execute('SELECT count(*) FROM albums WHERE name=?',('Expired album',)).fetchone()[0],0)

    def test_album_rename_is_atomic_preserves_private_members_provenance_and_queued_stamps(self):
        name='Private album'; self.library.edit(self.photo_id,'album_add',name)
        album=self.library.album_details(name)
        edits=dict(auto_albums={album['id']:['a'*64]},removed_albums=['Renamed','Elsewhere'],taken=True)
        self.library.db.execute('UPDATE photos SET edits=?,hidden=1,trashed=123,updated=2000000000,dirty=0 WHERE id=?',(packed(edits),self.photo_id))
        self.library.db.execute('UPDATE albums SET updated=2000000000,dirty=0 WHERE id=?',(album['id'],))
        self.library.db.execute("CREATE TRIGGER fail_migration BEFORE UPDATE OF albums ON photos BEGIN SELECT RAISE(ABORT,'fixture'); END")
        self.library.db.commit(); before=self.library.photo(self.photo_id)
        with self.assertRaisesRegex(LibraryError,'migrate'):
            self.library.rename_album(name,'Renamed')
        self.assertEqual(self.library.photo(self.photo_id),before)
        self.assertEqual(self.library.album_details(name)['dirty'],0)
        self.library.db.execute('DROP TRIGGER fail_migration');self.library.db.commit()
        self.library.rename_album(name,'Renamed')
        photo=self.library.photo(self.photo_id);new=self.library.album_details('Renamed')
        self.assertEqual(new['id'],album['id'])
        self.assertGreater(new['updated'],2000000000)
        self.assertGreater(photo['updated'],2000000000)
        self.assertEqual((photo['hidden'],photo['trashed'],photo['dirty']),(1,123,1))
        self.assertEqual(json.loads(photo['edits']),dict(auto_albums=edits['auto_albums'],removed_albums=['Elsewhere',name],taken=True))
        self.library.import_folders([self.source])
        self.assertIn('Renamed',json.loads(self.library.photo(self.photo_id)['albums']))
        self.assertNotIn(name,json.loads(self.library.photo(self.photo_id)['albums']))
        self.assertEqual(self.original.read_bytes(),self.original_bytes)

    def test_native_album_description_exact_limit_tombstone_and_cover_validation(self):
        name=self.library.edit_album('','create','Native empty')
        value='日本語\n\0'+('a'*8181)
        self.assertEqual(len(value.encode()),8192)
        self.library.edit_album(name,'description',value)
        self.assertEqual(self.library.album_details(name)['description'],value)
        before=self.library.album_details(name)
        for action,value in [('description','a'*8193),('cover',self.photo_id),('filter','is:favorite')]:
            with self.assertRaises(LibraryError):self.library.edit_album(name,action,value)
            self.assertEqual(self.library.album_details(name),before)
        self.library.edit_album(name,'delete_empty')
        with self.assertRaisesRegex(LibraryError,'retained'):
            self.library.edit_album('','create',name)
        self.library.edit(self.photo_id,'album_add','Target')
        with self.assertRaisesRegex(LibraryError,'retained'):
            self.library.rename_album('Target',name)

    def test_native_dates_locations_names_invalid_selection_and_rescan(self):
        self.library.edit(self.photo_id,'taken','2024-02-29T14:30:00.125+02:00')
        self.assertEqual(self.library.details(self.photo_id)['taken'],'2024-02-29T12:30:00.125000+00:00')
        self.library.edit(self.photo_id,'location','0, -180')
        self.library.edit(self.photo_id,'rename','旅行.png')
        before=self.library.photo(self.photo_id)
        for action,value in [('taken','2023-02-29'),('taken','2024-04-31'),('location','nan, 2'),('location','0, inf'),('rename','../private')]:
            with self.assertRaises(LibraryError):self.library.edit(self.photo_id,action,value)
            self.assertEqual(self.library.photo(self.photo_id),before)
        for action,value in [('taken','2025-01-01'),('location','1,2'),('rename','Changed.png')]:
            with self.assertRaises(LibraryError):self.library.edit_many([self.photo_id,'f'*64],action,value)
            self.assertEqual(self.library.photo(self.photo_id),before)
        self.library.import_folders([self.source])
        self.assertEqual(self.library.photo(self.photo_id)['name'],'旅行.png')
        self.assertEqual(self.library.details(self.photo_id)['location'],'0.0, -180.0')
        self.library.edit(self.photo_id,'location','')
        self.library.import_folders([self.source])
        self.assertEqual(self.library.details(self.photo_id)['location'],'')
        self.assertEqual(self.original.read_bytes(),self.original_bytes)

    def test_native_parser_rejects_raw_invalid_values_and_keeps_coordinate_precision(self):
        before=self.library.photo(self.photo_id)
        for action,value in [('taken',2),('taken','2024-02-30T00:00:00Z'),('taken','2024-01-01T23:59:60Z'),('location',None),('location','1,2,3'),('location','1\0,2')]:
            child=self.child(action,value)
            self.assertIn('error',json.loads(child.stdout.readline()))
            self.assertEqual(child.wait(timeout=3),1)
            self.assertEqual(self.library.photo(self.photo_id),before)
        self.library.edit(self.photo_id,'location','+12.123456789012345, -0.000000000012345')
        geo=json.loads(self.library.photo(self.photo_id)['metadata'])['geoData']
        self.assertEqual(geo,dict(latitude=12.123456789012345,longitude=-0.000000000012345))
        self.library.edit(self.photo_id,'taken','2024-02-29 14:30+02:00')
        self.assertEqual(self.library.details(self.photo_id)['taken'],'2024-02-29T12:30:00+00:00')


if __name__=='__main__':unittest.main()
