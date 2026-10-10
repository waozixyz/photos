"""Native camera preparation releases allocations across retries and revocation."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = ROOT / 'build/backup-staging-test/memory-test'


class BackupStagingMemoryTest(unittest.TestCase):
    def check_memory(self, cancel, content):
        with tempfile.TemporaryDirectory(prefix='memory-', dir=PROGRAM.parent) as temporary:
            folder = Path(temporary)
            source = folder / 'source'
            source.write_bytes(content)
            stage = folder / 'staged'
            stage.mkdir(mode=0o700)
            result = subprocess.run([str(PROGRAM), str(source), str(stage),
                                     '1' if cancel else '0'], capture_output=True,
                                    text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stderr, '')
            self.assertLessEqual(int(result.stdout.strip().split('=', 1)[1]), 32768)
            self.assertEqual(source.read_bytes(), content)
            if cancel:
                self.assertEqual(list(stage.iterdir()), [])
            else:
                self.assertEqual(len(list(stage.iterdir())), 1)
                self.assertFalse(list(stage.glob('.stage-*')))

    def test_successful_retries_release_transient_allocations(self):
        for content in (b'', b'\x00binary synthetic original\xff'):
            with self.subTest(empty=not content):
                self.check_memory(False, content)

    def test_permission_cancellation_releases_transient_allocations(self):
        for content in (b'', b'\x00binary synthetic original\xff'):
            with self.subTest(empty=not content):
                self.check_memory(True, content)
