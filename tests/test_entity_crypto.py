"""The native entity helper derives MeshClient's published keys and chunks."""
import hashlib
import hmac
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from photos import entity_crypto


class EntityCryptoTest(unittest.TestCase):
    private = bytes(i % 256 for i in range(2560))

    def test_record_keys_match_mesh_client(self):
        self.assertEqual(entity_crypto.record_key(self.private, 'photos').hex(),
                         'd3d54fa5cc5ead0b135a9a1506a3838c86b53b30b99a925cb52e1b18405cadd1')
        self.assertEqual(entity_crypto.record_key(self.private, 'levels').hex(),
                         '1401bd0b55397f1bf59de1831dabe6f18290d7a0a1b14cfcd382502d77cd01a0')
        self.assertEqual(entity_crypto.record_key(self.private, 'albums'),
                         hmac.new(self.private, b'daochi-entity-v1/records/albums', hashlib.sha256).digest())
        for kind in ('Photos', '', 'photos/x'):
            with self.assertRaises(ValueError):
                entity_crypto.record_key(self.private, kind)

    def test_two_chunk_photo_seals_to_mesh_client_addresses(self):
        data = bytes((i * 7 + 3) % 256 for i in range(entity_crypto.CHUNK + 5))
        digest, sealed = entity_crypto.seal(self.private, 'photos', data)
        self.assertEqual(digest, '102a8ba0d321f0ac40bf067b1399397dc926919c4d2645a2617e568d3e37cd36')
        self.assertEqual([hashlib.sha256(chunk).hexdigest() for chunk in sealed],
                         ['01679c4afcf6758f046775113d544a72d56dd38d4ce4165c73e26feb63338d83',
                          'aa3d34eb39c53546a30c18e1415cbe8c749b565a3b60a4709d7fc6ff9efa1471'])
        self.assertEqual([len(chunk) for chunk in sealed], [entity_crypto.CHUNK + 16, 21])
        opened = b''.join(entity_crypto.open_chunk(self.private, 'photos', digest, index, chunk)
                          for index, chunk in enumerate(sealed))
        self.assertEqual(opened, data)

    def test_moved_or_altered_chunks_do_not_open(self):
        digest, sealed = entity_crypto.seal(self.private, 'photos', b'a small photo')
        altered = bytearray(sealed[0]); altered[0] ^= 1
        for index, chunk, kind, other in ((1, sealed[0], 'photos', digest), (0, bytes(altered), 'photos', digest),
                                          (0, sealed[0], 'levels', digest), (0, sealed[0], 'photos', 'a' * 64)):
            with self.assertRaisesRegex(ValueError, 'authentication'):
                entity_crypto.open_chunk(self.private, kind, other, index, chunk)


if __name__ == '__main__':
    unittest.main()
