import base64
import copy
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import tempfile
import time
import unittest

from PIL import Image
from photos import importing, people, auto_albums
from photos.library import Library, LibraryError, digest_file, packed


class FaceImportTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.source = self.root/'Camera'; self.source.mkdir()
        self.path = self.source/'旅行.png'; Image.new('RGB', (12, 9), 'green').save(self.path)
        self.original = self.path.read_bytes(); self.identity = digest_file(self.path)
        self.library = Library(self.root/'library'); self.addCleanup(self.library.db.close)
        self.person = dict(id='b'*64, name='日本語 Ada', ignored=0, updated=1, birth='1990-06-15')
        self.index = dict(model=people.MODEL, scanned=True, tags=[self.person], faces=[dict(
            id='c'*64, person=self.person, rect=[0, 10, 5000, 4000], excluded=['d'*64],
            vector=base64.b64encode(struct.pack('<128f', 1., *([0.]*127))).decode())])

    def request(self, index=None, **changes):
        stat = self.path.stat()
        value = dict(id=self.identity, name=self.path.name, extension='.png', kind='photo',
            size=stat.st_size, taken=1600000000., width=12, height=9, metadata={'unknown': None},
            albums=['Camera'], favorite=False, archived=False, caption='', tags='', correct_date=False,
            path=str(self.path), mtime=stat.st_mtime_ns, stamp='metadata:exact',
            people_index=self.index if index is None else index, auto_memberships={'Camera': [self.person['id']]})
        value.update(changes); return value

    def counts(self):
        return {table: self.library.db.execute('SELECT count(*) FROM '+table).fetchone()[0]
                for table in ['photos', 'albums', 'sources', 'people', 'faces', 'face_scans', 'person_tags', 'person_tag_edits']}

    def test_native_photo_face_tags_provenance_and_source_publish_together(self):
        self.assertEqual(importing.merge(self.library, self.request(), report=True), dict(saved=1, added=1, warning=0))
        self.assertEqual(people.export_index(self.library, self.identity), self.index)
        self.assertEqual(auto_albums.export_memberships(self.library, self.library.photo(self.identity)),
                         {'Camera': [self.person['id']]})
        self.assertEqual(json.loads(self.library.photo(self.identity)['metadata']), {'unknown': None})
        self.assertEqual(self.library.db.execute('SELECT meta_stamp FROM sources').fetchone()[0], 'metadata:exact')
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_sidecar_import_retains_private_index_outside_ordinary_metadata(self):
        sidecar = self.path.with_name(self.path.name+'.json')
        sidecar.write_text(packed(dict(photosPeople=self.index, photosAutoAlbums={'Camera': [self.person['id']]}, unknown=None)))
        original_sidecar = sidecar.read_bytes()
        self.assertEqual(self.library.import_file(self.path, self.source), 'added')
        self.assertEqual(people.export_index(self.library, self.identity), self.index)
        self.assertEqual(json.loads(self.library.photo(self.identity)['metadata']), {'unknown': None})
        self.assertEqual(sidecar.read_bytes(), original_sidecar)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_invalid_whole_index_warns_and_retains_original_without_partial_people(self):
        variants = [[], {}, {**self.index, 'model': 'other'}, {**self.index, 'scanned': 1},
                    {**self.index, 'scanned': False}, {**self.index, 'tags': [self.person]*2},
                    {**self.index, 'tags': [{**self.person, 'name': ''}]},
                    {**self.index, 'faces': self.index['faces']*2}]
        for change in [dict(id='c'*64+'\0'), dict(person={}), dict(rect=[True, 0, 1, 1]),
                       dict(rect=[9999, 0, 2, 1]), dict(vector='!'*684),
                       dict(vector=base64.b64encode(struct.pack('<128f', float('nan'), *([0.]*127))).decode()),
                       dict(vector=base64.b64encode(bytes(512)).decode()), dict(excluded=['d'*64+'\0'])]:
            variants.append({**self.index, 'faces': [{**self.index['faces'][0], **change}]})
        for change in [dict(name='bad\0name'), dict(ignored=True), dict(updated=-1),
                       dict(updated=32503680000), dict(birth='2023-02-29'), dict(birth=None),
                       dict(birth='1599-12-31'), dict(birth='9999-01-01')]:
            variants.append({**self.index, 'tags': [{**self.person, **change}]})
        for number, index in enumerate(variants):
            peer = Library(self.root/f'invalid-{number}')
            try:
                result = importing.merge(peer, self.request(index), report=True)
                self.assertEqual(result, dict(saved=1, added=1, warning=1), index)
                self.assertEqual(peer.db.execute('SELECT count(*) FROM photos').fetchone()[0], 1)
                for table in ['people', 'faces', 'face_scans', 'person_tags', 'person_tag_edits']:
                    self.assertEqual(peer.db.execute('SELECT count(*) FROM '+table).fetchone()[0], 0, (number, table))
                self.assertNotIn('photosPeople', peer.photo(self.identity)['metadata'])
            finally: peer.db.close()
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_newer_people_and_missing_legacy_birthdays_survive_import(self):
        self.library.db.execute('INSERT INTO people VALUES(?,?,?,?,?)',
            (self.person['id'], 'Later correction', 1, 10, '1980-01-01')); self.library.db.commit()
        self.assertEqual(importing.merge(self.library, self.request(), report=True)['warning'], 0)
        self.assertEqual(people.group(self.library, self.person['id']),
                         {**self.person, 'name': 'Later correction', 'ignored': 1, 'updated': 10., 'birth': '1980-01-01'})
        legacy = copy.deepcopy(self.index); legacy['faces'][0]['person'].pop('birth')
        legacy['faces'][0]['person']['updated'] = 11
        second = self.source/'second.png'; Image.new('RGB', (13, 9), 'green').save(second)
        legacy['faces'][0]['id'] = 'e'*64
        result = importing.merge(self.library, self.request(legacy, id=digest_file(second), path=str(second)), report=True)
        self.assertEqual(result['warning'], 0)
        self.assertEqual(people.group(self.library, self.person['id'])['birth'], '1980-01-01')

    def test_manual_tags_do_not_mark_face_scan_and_legacy_empty_scan_does(self):
        self.index.update(scanned=False, faces=[])
        self.assertEqual(importing.merge(self.library, self.request(), report=True)['warning'], 0)
        self.assertEqual(people.export_index(self.library, self.identity), self.index)
        self.assertEqual(self.counts()['face_scans'], 0)
        peer = Library(self.root/'legacy'); self.addCleanup(peer.db.close)
        self.assertEqual(importing.merge(peer, self.request(dict(model=people.MODEL, faces=[])), report=True)['warning'], 0)
        self.assertEqual(people.export_index(peer, self.identity), dict(model=people.MODEL, scanned=True, faces=[], tags=[]))

    def test_existing_face_identity_and_rescan_never_overwrite_corrections(self):
        importing.merge(self.library, self.request())
        before = people.export_index(self.library, self.identity)
        changed = copy.deepcopy(self.index); changed['faces'][0]['person']['name'] = 'Old import'
        self.assertEqual(importing.merge(self.library, self.request(changed), report=True), dict(saved=1, added=0, warning=0))
        self.assertEqual(people.export_index(self.library, self.identity), before)
        second = self.source/'second.png'; Image.new('RGB', (13, 9), 'green').save(second)
        self.assertEqual(importing.merge(self.library, self.request(id=digest_file(second), path=str(second)), report=True)['warning'], 1)
        self.assertEqual(people.export_index(self.library, self.identity), before)
        self.assertIsNone(people.export_index(self.library, digest_file(second)))

    def test_private_or_source_write_failure_rolls_back_every_table(self):
        for table in ['people', 'faces', 'face_scans', 'person_tags', 'sources']:
            with self.subTest(table=table):
                self.library.db.execute(f"CREATE TRIGGER fail BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'fixture'); END")
                self.library.db.commit()
                with self.assertRaises(LibraryError): importing.merge(self.library, self.request())
                self.assertEqual(set(self.counts().values()), {0})
                self.library.db.execute('DROP TRIGGER fail'); self.library.db.commit()
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_killed_native_face_writer_rolls_back_original_metadata_and_indexes(self):
        self.library.db.execute("""CREATE TRIGGER slow_face BEFORE INSERT ON faces BEGIN
            SELECT sum(x) FROM (WITH RECURSIVE work(x) AS
                (SELECT 1 UNION ALL SELECT x+1 FROM work WHERE x<100000000) SELECT x FROM work); END""")
        self.library.db.commit()
        child = subprocess.Popen([str(importing.metadata_program()), str(self.library.root/'library.sqlite')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={k:v for k,v in os.environ.items() if k not in {
                'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'}})
        def close():
            if child.poll() is None: child.kill()
            child.wait(timeout=3)
            if not child.stdin.closed: child.stdin.close()
            child.stdout.close(); child.stderr.close()
        self.addCleanup(close)
        child.stdin.write(packed(self.request()).encode()); child.stdin.close()
        self.library.db.execute('PRAGMA busy_timeout=10')
        deadline = time.monotonic()+3; locked = False
        while time.monotonic() < deadline:
            try: self.library.db.execute('BEGIN IMMEDIATE'); self.library.db.rollback()
            except sqlite3.OperationalError: locked = True; break
            time.sleep(.01)
        self.assertTrue(locked, 'Native face writer never acquired its transaction')
        child.kill(); child.wait(timeout=3)
        self.assertEqual(set(self.counts().values()), {0})
        self.assertEqual(self.path.read_bytes(), self.original)
