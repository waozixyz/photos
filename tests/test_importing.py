import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from photos import importing
from PIL import Image
from photos.library import Library, LibraryError, digest_file, packed


class OriginalImportTest(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.source=self.root/'source';self.destination=self.root/'stored'
        self.data=bytes(range(256))*10001;self.source.write_bytes(self.data)
        self.identity=hashlib.sha256(self.data).hexdigest()

    def test_streamed_original_exact_bytes_private_mode_and_repeat(self):
        importing.copy(self.source,self.destination,self.identity)
        self.assertEqual(self.destination.read_bytes(),self.data)
        self.assertEqual(self.destination.stat().st_mode & 0o777,0o600)
        modified=self.destination.stat().st_mtime_ns
        importing.copy(self.source,self.destination,self.identity)
        self.assertEqual(self.destination.stat().st_mtime_ns,modified)
        self.assertFalse(list(self.root.glob('stored.import-*')))

    def test_size_snapshot_change_cannot_publish_a_different_original(self):
        for expected in [len(self.data)-1,len(self.data)+1]:
            with self.assertRaisesRegex(LibraryError,'changed'):
                importing.copy(self.source,self.destination,self.identity,expected)
            self.assertFalse(self.destination.exists())
            self.assertFalse(list(self.root.glob('stored.import-*')))
        self.assertEqual(self.source.read_bytes(),self.data)

    def test_empty_original_mismatch_and_existing_conflict_keep_sources(self):
        self.source.write_bytes(b'')
        importing.copy(self.source,self.destination,digest_file(self.source))
        self.assertEqual(self.destination.read_bytes(),b'')
        self.destination.write_bytes(b'Keep existing bytes')
        with self.assertRaisesRegex(LibraryError,'preserved'):
            importing.copy(self.source,self.destination,digest_file(self.source))
        self.assertEqual(self.destination.read_bytes(),b'Keep existing bytes')
        self.destination.unlink()
        with self.assertRaisesRegex(LibraryError,'changed'):
            importing.copy(self.source,self.destination,'a'*64)
        self.assertFalse(self.destination.exists())
        self.assertFalse(list(self.root.glob('stored.import-*')))

    def test_links_and_interrupted_native_reader_publish_nothing(self):
        link=self.root/'link';link.symlink_to(self.source)
        with self.assertRaisesRegex(LibraryError,'links'):
            importing.copy(link,self.destination,self.identity)
        self.destination.symlink_to(self.source)
        with self.assertRaisesRegex(LibraryError,'links'):
            importing.copy(self.source,self.destination,self.identity)
        self.destination.unlink()
        pipe=self.root/'pipe';os.mkfifo(pipe)
        actual_run=subprocess.run
        def interrupted(*args,**kwargs):
            kwargs['timeout']=.1
            return actual_run(*args,**kwargs)
        with patch('photos.importing.subprocess.run',side_effect=interrupted):
            with self.assertRaisesRegex(LibraryError,'could not finish'):
                importing.copy(pipe,self.destination,self.identity)
        self.assertFalse(self.destination.exists())
        self.assertFalse(list(self.root.glob('stored.import-*')))
        self.assertEqual(self.source.read_bytes(),self.data)


class MetadataImportTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.source = self.root/'Camera'; self.source.mkdir()
        self.path = self.source/'旅行.png'; Image.new('RGB', (12, 9), 'green').save(self.path)
        self.library = Library(self.root/'library'); self.addCleanup(self.library.db.close)
        self.identity = digest_file(self.path); self.original = self.path.read_bytes()

    def request(self, **changes):
        stat = self.path.stat()
        value = dict(id=self.identity, name=self.path.name, extension='.png', kind='photo',
                     size=stat.st_size, taken=1600000000.0, width=12, height=9,
                     metadata={}, albums=['Camera'], favorite=False, archived=False,
                     caption='', tags='', correct_date=False, path=str(self.path),
                     mtime=stat.st_mtime_ns, stamp='')
        value.update(changes); return value

    def test_native_large_unknown_metadata_null_huge_integers_unicode_and_exact_stamp(self):
        nested = {'integer': 10**400, 'null': None, 'yes': True, 'no': False,
                  'values': [None, '', 'é日本語\n\0', {'precision': 123456789012345678901234567890}]}
        # Values deeper than the display scanner's 64 levels remain opaque.
        deep = None
        for _ in range(80): deep = [deep]
        metadata = {'unknown': nested, '': deep, 'a\0b': 'Keep NUL key',
                    'quoted"key': {'raw': 10**100}, '日本語': 'é'*1000000}
        caption = 'é日本語\n\0'+'x'*20000
        self.assertTrue(importing.merge(self.library, self.request(metadata=metadata, caption=caption,
            mtime=1791540000123456789, stamp='metadata:123:1791540000987654321')))
        row = self.library.photo(self.identity)
        self.assertEqual(row['metadata'], packed(metadata))
        self.assertEqual(row['caption'], caption)
        self.assertEqual(self.library.db.execute('SELECT mtime,meta_stamp FROM sources').fetchone()[:],
                         (1791540000123456789, 'metadata:123:1791540000987654321'))
        self.library.db.execute('UPDATE photos SET dirty=0 WHERE id=?', (self.identity,)); self.library.db.commit()
        before = self.library.photo(self.identity)
        self.assertFalse(importing.merge(self.library, self.request(metadata=metadata, caption='Ignore replacement')))
        self.assertEqual(self.library.photo(self.identity), before)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_native_merge_keeps_corrections_private_state_and_monotonic_backup_stamp(self):
        self.library.import_folders([self.source])
        self.library.edit(self.identity, 'caption', 'Edited caption')
        self.library.edit(self.identity, 'taken', '2024-06-15')
        self.library.edit(self.identity, 'location', '0, -180')
        self.library.edit(self.identity, 'album_remove', 'Camera')
        self.library.edit(self.identity, 'album_add', 'Renamed')
        self.library.db.execute('UPDATE photos SET hidden=1,trashed=123,updated=2000000000,dirty=0 WHERE id=?',
                                (self.identity,)); self.library.db.commit()
        incoming = dict(description='Source caption', photoTakenTime={'timestamp': '1600000000'},
                        geoData={'latitude': 8, 'longitude': 9}, geoDataExif=None, unknown=None)
        self.assertFalse(importing.merge(self.library, self.request(metadata=incoming, correct_date=True,
                            albums=['Camera', 'Favorites'], favorite=True, caption='Source caption')))
        row = self.library.photo(self.identity)
        self.assertEqual((row['hidden'], row['trashed'], row['caption'], row['taken']),
                         (1, 123, 'Edited caption', 1718409600))
        self.assertGreater(row['updated'], 2000000000)
        self.assertEqual(json.loads(row['albums']), ['Favorites', 'Renamed'])
        saved = json.loads(row['metadata'])
        self.assertEqual(saved['geoData'], {'latitude': 0, 'longitude': -180})
        self.assertNotIn('geoDataExif', saved); self.assertIn('unknown', saved)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_source_failure_rolls_back_photo_album_and_source_as_one_transaction(self):
        self.library.db.execute("CREATE TRIGGER fail_source BEFORE INSERT ON sources BEGIN SELECT RAISE(ABORT,'fixture'); END")
        self.library.db.commit()
        with self.assertRaisesRegex(LibraryError, 'source'):
            importing.merge(self.library, self.request())
        self.assertEqual(self.library.db.execute('SELECT count(*) FROM photos').fetchone()[0], 0)
        self.assertEqual(self.library.db.execute('SELECT count(*) FROM albums').fetchone()[0], 0)
        self.assertEqual(self.library.db.execute('SELECT count(*) FROM sources').fetchone()[0], 0)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_killed_actual_native_writer_rolls_back_album_photo_and_cache(self):
        self.library.db.execute("""CREATE TRIGGER slow_source BEFORE INSERT ON sources BEGIN
            SELECT sum(x) FROM (WITH RECURSIVE work(x) AS
                (SELECT 1 UNION ALL SELECT x+1 FROM work WHERE x<100000000) SELECT x FROM work);
            END""")
        self.library.db.commit()
        child = subprocess.Popen([str(importing.metadata_program()), str(self.library.root/'library.sqlite')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={k: v for k, v in os.environ.items() if k not in {
                'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'}})
        def close():
            if child.poll() is None: child.kill()
            child.wait(timeout=3)
            if not child.stdin.closed: child.stdin.close()
            child.stdout.close(); child.stderr.close()
        self.addCleanup(close)
        child.stdin.write(packed(self.request()).encode()); child.stdin.close()
        self.library.db.execute('PRAGMA busy_timeout=10')
        import sqlite3
        locked = False; deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            try:
                self.library.db.execute('BEGIN IMMEDIATE'); self.library.db.rollback()
            except sqlite3.OperationalError:
                locked = True; break
            time.sleep(.01)
        self.assertTrue(locked, 'Native writer never acquired the transaction')
        child.kill(); child.wait(timeout=3)
        self.assertEqual(self.library.db.execute('SELECT count(*) FROM photos').fetchone()[0], 0)
        self.assertEqual(self.library.db.execute('SELECT count(*) FROM albums').fetchone()[0], 0)
        self.assertEqual(self.library.db.execute('SELECT count(*) FROM sources').fetchone()[0], 0)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_invalid_native_request_and_pending_parent_transaction_change_nothing(self):
        for change in [dict(id='../outside'), dict(metadata=[]), dict(size='12'), dict(mtime=1.2),
                       dict(size=2**63), dict(mtime=2**63), dict(albums=[False]),
                       dict(caption=False), dict(metadata={'photosPeople': {}})]:
            with self.assertRaises(LibraryError): importing.merge(self.library, self.request(**change))
        self.assertEqual(self.library.db.execute('SELECT count(*) FROM photos').fetchone()[0], 0)
        self.library.db.execute("INSERT INTO settings VALUES('pending','true')")
        with self.assertRaisesRegex(LibraryError, 'current library operation'):
            importing.merge(self.library, self.request())
        self.assertTrue(self.library.db.in_transaction); self.library.db.rollback()


if __name__=='__main__':unittest.main()
