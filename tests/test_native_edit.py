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


if __name__=='__main__':unittest.main()
