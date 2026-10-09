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
from photos import live, native_edit, semantic
from photos.library import Library, LibraryError, digest_file, packed


class NativeSemanticIndexTest(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.source=self.root/'source';self.source.mkdir()
        self.files=[]
        for i in range(2):
            path=self.source/f'Photo {i}.png';Image.new('RGB',(20,20),(100+i*30,100,200)).save(path);self.files.append(path)
        self.library=Library(self.root/'library');self.addCleanup(self.library.db.close)
        self.library.import_folders([self.source]);self.ids=[digest_file(p) for p in self.files]
        self.blob=struct.pack('<512f',1.0,*([0.0]*511))
        self.library.set_setting('semantic_enabled',True);self.library.set_setting('semantic_processing',True)

    def publish(self,identity=None,blob=None,photo=None):
        return native_edit.semantic_index(self.library,'publish',photo or dict(self.library.photo(identity or self.ids[0])),self.blob if blob is None else blob)

    def stored(self,identity=None):
        return self.library.db.execute('SELECT * FROM semantic_embeddings WHERE photo_id=?',(identity or self.ids[0],)).fetchone()

    def test_native_publication_next_and_retry_keep_exact_vectors_and_backup_progress(self):
        self.library.db.execute('UPDATE photos SET dirty=0,updated=32503679000');self.library.db.commit()
        self.assertIn(native_edit.semantic_index(self.library,'next'),self.ids)
        self.assertTrue(self.publish())
        self.assertEqual(self.stored()['embedding'],self.blob)
        self.assertGreater(self.library.photo(self.ids[0])['updated'],32503679000)
        self.assertTrue(self.library.photo(self.ids[0])['dirty'])
        second=dict(self.library.photo(self.ids[1]))
        self.assertTrue(native_edit.semantic_index(self.library,'failure',second))
        self.assertIsNone(self.stored(self.ids[1])['embedding'])
        self.assertFalse(self.library.photo(self.ids[1])['dirty'])
        self.assertIsNone(native_edit.semantic_index(self.library,'next'))
        self.assertTrue(native_edit.semantic_index(self.library,'retry'))
        self.assertEqual(self.stored()['embedding'],self.blob)
        self.assertIsNone(self.stored(self.ids[1]))
        self.assertEqual(native_edit.semantic_index(self.library,'next'),self.ids[1])

    def test_late_scan_never_overwrites_restored_indexes_or_more_recent_edits(self):
        before=dict(self.library.photo(self.ids[0]))
        restored=struct.pack('<512f',0.0,1.0,*([0.0]*510))
        self.library.db.execute('INSERT INTO semantic_embeddings VALUES(?,?,?,?)',(self.ids[0],semantic.MODEL,restored,''));self.library.db.commit()
        self.assertFalse(self.publish(photo=before))
        self.assertEqual(self.stored()['embedding'],restored)
        self.library.db.execute('DELETE FROM semantic_embeddings');self.library.db.commit()
        self.library.edit(self.ids[0],'caption','New caption while scanning')
        current=dict(self.library.photo(self.ids[0]));self.assertFalse(self.publish(photo=before))
        self.assertIsNone(self.stored());self.assertEqual(dict(self.library.photo(self.ids[0])),current)
        self.assertTrue(self.publish())

    def test_paused_disabled_hidden_trashed_and_private_pair_do_not_publish_or_loop(self):
        for action in ['pause','disable','hide','trash']:
            with self.subTest(action=action):
                before=dict(self.library.photo(self.ids[0]))
                if action=='pause':self.library.set_setting('semantic_processing',False)
                if action=='disable':self.library.set_setting('semantic_enabled',False)
                if action=='hide':self.library.edit(self.ids[0],'hidden_set','1')
                if action=='trash':self.library.edit(self.ids[0],'trash')
                self.assertFalse(self.publish(photo=before));self.assertIsNone(self.stored())
                self.library.set_setting('semantic_enabled',True);self.library.set_setting('semantic_processing',True)
                self.library.edit(self.ids[0],'hidden_set','0');self.library.edit(self.ids[0],'restore')
        movie=self.source/'Motion.mp4';movie.write_bytes(b'Private synthetic motion')
        self.library.import_folders([self.source]);identity=digest_file(movie);live.pair(self.library,self.ids[0],identity)
        self.library.db.execute('UPDATE photos SET hidden=1 WHERE id=?',(identity,));self.library.db.commit()
        self.assertFalse(self.publish())
        self.assertEqual(native_edit.semantic_index(self.library,'next'),self.ids[1])
        self.assertTrue(self.publish(self.ids[1]))
        self.assertIsNone(native_edit.semantic_index(self.library,'next'))

    def test_invalid_vectors_failed_writes_or_missing_helper_preserve_the_photo(self):
        before=dict(self.library.photo(self.ids[0]))
        for value in [b'short',struct.pack('<512f',float('nan'),*([0.0]*511)),struct.pack('<512f',*([1.0]*512))]:
            with self.assertRaises(LibraryError):self.publish(blob=value)
            self.assertIsNone(self.stored())
        self.library.db.execute("CREATE TRIGGER stop_index BEFORE UPDATE OF dirty ON photos BEGIN SELECT RAISE(ABORT,'fixture failure'); END");self.library.db.commit()
        with self.assertRaises(LibraryError):self.publish()
        self.assertIsNone(self.stored());self.assertEqual(dict(self.library.photo(self.ids[0])),before)
        with patch.object(native_edit,'program',return_value=self.root/'missing'):
            with self.assertRaises(LibraryError):self.publish()
        self.assertEqual(dict(self.library.photo(self.ids[0])),before)

    def test_worker_uses_native_selection_and_publication(self):
        calls=[];original=native_edit.semantic_index
        def record(*arguments,**keywords):calls.append(arguments[1]);return original(*arguments,**keywords)
        before={p:p.read_bytes() for p in self.files}
        with patch.object(semantic,'prepare'),patch.object(semantic,'image_tensor',return_value=b'synthetic tensor'),patch.object(semantic,'infer',return_value=self.blob),patch.object(native_edit,'semantic_index',side_effect=record):
            semantic._run(str(self.library.root))
        self.assertEqual(calls,['next','publish','next','publish','next'])
        self.assertTrue(all(self.stored(i)['embedding']==self.blob for i in self.ids))
        for path,data in before.items():
            self.assertEqual(path.read_bytes(),data)
            self.assertEqual(self.library.original(self.library.photo(digest_file(path))).read_bytes(),data)

    def test_native_failed_publication_and_retry_roll_back_on_interruption(self):
        # A malformed or truncated helper request cannot publish an index.
        program=native_edit.program()
        for message in [b'{"action":"semantic_index"',packed(dict(action='semantic_index',operation='publish',photo_id=self.ids[0],expected='invalid',value=self.blob.hex())).encode()+b'\n']:
            result=subprocess.run([str(program),str(self.library.root/'library.sqlite')],input=message,capture_output=True,timeout=5,
                env={k:v for k,v in os.environ.items() if k not in {'DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','DBUS_SESSION_BUS_ADDRESS'}})
            self.assertNotEqual(result.returncode,0);self.assertIsNone(self.stored())
        self.library.db.execute("CREATE TRIGGER slow_index BEFORE UPDATE OF dirty ON photos BEGIN SELECT sum(x) FROM (WITH RECURSIVE work(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM work WHERE x<100000000) SELECT x FROM work); END");self.library.db.commit()
        before=dict(self.library.photo(self.ids[0]))
        message=packed(dict(action='semantic_index',operation='publish',photo_id=self.ids[0],expected=struct.pack('<d',before['updated']).hex(),value=self.blob.hex())).encode()+b'\n'
        child=subprocess.Popen([str(program),str(self.library.root/'library.sqlite')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            env={k:v for k,v in os.environ.items() if k not in {'DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','DBUS_SESSION_BUS_ADDRESS'}})
        try:
            child.stdin.write(message);child.stdin.flush();time.sleep(.15)
            self.assertIsNone(child.poll())
            child.kill();child.wait(timeout=5)
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=5)
            child.stdin.close();child.stdout.close();child.stderr.close()
        self.assertIsNone(self.stored());self.assertEqual(dict(self.library.photo(self.ids[0])),before)

    def test_native_retry_failure_keeps_all_failed_and_valid_indexes(self):
        self.assertTrue(self.publish())
        self.assertTrue(native_edit.semantic_index(self.library,'failure',dict(self.library.photo(self.ids[1]))))
        self.library.db.execute("CREATE TRIGGER stop_retry BEFORE DELETE ON semantic_embeddings BEGIN SELECT RAISE(ABORT,'fixture failure'); END");self.library.db.commit()
        with self.assertRaises(LibraryError):native_edit.semantic_index(self.library,'retry')
        self.assertEqual(self.stored()['embedding'],self.blob)
        self.assertTrue(self.stored(self.ids[1])['error'])
