import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from photos import importing
from photos.library import LibraryError, digest_file


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


if __name__=='__main__':unittest.main()
