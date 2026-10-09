"""Native encryption compatibility, failure isolation and resumable chunks."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import struct
import subprocess
import unittest
from unittest.mock import patch

from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_encrypt as reference_encrypt
from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_decrypt as reference_decrypt
from nacl.signing import SigningKey, VerifyKey
from photos import sync_crypto as crypto


class SyncCryptoTest(unittest.TestCase):
    key = bytes(range(32))
    nonce = bytes(range(24))
    aad = b'{"account_id":"fixture","app_id":"photos","id":"media","part":"chunk:0"}'

    def test_fixed_nonce_empty_binary_unicode_and_whole_chunk_interoperate(self):
        for plain in [b'', b'\0\xff\0\xfe', '家族 📷 café'.encode(), bytes(range(256))*32768]:
            cipher = crypto.encrypt(plain, self.aad, self.nonce, self.key)
            self.assertEqual(cipher, reference_encrypt(plain,self.aad,self.nonce,self.key))
            self.assertEqual(reference_decrypt(cipher,self.aad,self.nonce,self.key), plain)
            self.assertEqual(crypto.decrypt(cipher,self.aad,self.nonce,self.key), plain)
        self.assertEqual(crypto.decrypt(crypto.encrypt(b'empty AAD',b'',self.nonce,self.key),b'',self.nonce,self.key),b'empty AAD')

    def test_blob_retries_match_old_ciphertext_and_bind_changed_bytes_and_addresses(self):
        plain = b'original unchanged photo bytes'
        nonce = hmac.new(self.key,self.aad+hashlib.sha256(plain).digest(),hashlib.sha256).digest()[:24]
        expected = nonce+reference_encrypt(plain,self.aad,nonce,self.key)
        first = crypto.seal_blob(plain,self.aad,self.key)
        self.assertEqual(first, expected)
        self.assertEqual(crypto.seal_blob(plain,self.aad,self.key),first)
        self.assertNotEqual(crypto.seal_blob(plain+b'changed',self.aad,self.key)[:24],first[:24])
        self.assertNotEqual(crypto.seal_blob(plain,self.aad+b'other address',self.key)[:24],first[:24])

    def test_records_generate_native_random_nonces_and_remain_interoperable(self):
        plain = b'{"id":"fixture","caption":"private"}'
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda _:crypto.seal_record(plain,self.aad,self.key),range(16)))
        self.assertEqual(len({value[:24] for value in values}),16)
        for value in values:
            self.assertEqual(reference_decrypt(value[24:],self.aad,value[:24],self.key),plain)

    def test_wrong_key_nonce_aad_truncation_and_modified_tag_publish_no_plaintext(self):
        cipher = reference_encrypt(b'secret fixture only',self.aad,self.nonce,self.key)
        cases = [(cipher,self.aad,self.nonce,b'x'*32), (cipher,self.aad,b'x'*24,self.key),
                 (cipher,self.aad+b'x',self.nonce,self.key), (cipher[:-1],self.aad,self.nonce,self.key),
                 (cipher[:-1]+bytes([cipher[-1]^1]),self.aad,self.nonce,self.key)]
        for body,aad,nonce,key in cases:
            request = struct.pack('!BBII',1,2,len(body),len(aad))+key+nonce+aad+body
            result = subprocess.run([str(crypto.program())],input=request,capture_output=True,timeout=5)
            self.assertEqual(result.returncode,1)
            self.assertEqual(result.stdout,b'');self.assertEqual(result.stderr,b'')
            with self.assertRaisesRegex(ValueError,'authentication'):
                crypto.decrypt(body,aad,nonce,key)

    def test_malformed_binary_headers_sizes_trailing_data_and_partial_input_fail_silently(self):
        valid = struct.pack('!BBII',1,1,3,1)+self.key+self.nonce+b'a'+b'abc'
        cases = [b'',valid[:65],b'\0'+valid[1:],valid[:1]+b'\10'+valid[2:],valid+b'extra',valid[:-1],
                 struct.pack('!BBII',1,1,crypto.LIMIT+1,0)+self.key+self.nonce,
                 struct.pack('!BBII',1,1,0,1024*1024+1)+self.key+self.nonce,
                 struct.pack('!BBII',1,2,15,0)+self.key+self.nonce]
        for request in cases:
            result = subprocess.run([str(crypto.program())],input=request,capture_output=True,timeout=5)
            self.assertEqual(result.returncode,2)
            self.assertEqual(result.stdout,b'');self.assertEqual(result.stderr,b'')
        for key,nonce in [(self.key[:-1],self.nonce),(self.key,self.nonce[:-1])]:
            with self.assertRaises(ValueError):crypto.encrypt(b'x',b'',nonce,key)

    def test_unavailable_or_interrupted_native_engine_never_uses_a_fallback(self):
        for error in [OSError(),subprocess.TimeoutExpired('native encryption',30)]:
            with patch.object(crypto.subprocess,'run',side_effect=error):
                with self.assertRaisesRegex(ValueError,'preserved'):
                    crypto.encrypt(b'fixture',self.aad,self.nonce,self.key)

    def test_device_signatures_public_keys_and_new_seeds_preserve_existing_identities(self):
        expected = SigningKey(self.key)
        self.assertEqual(crypto.device_public(self.key), bytes(expected.verify_key))
        for message in [b'',b'daochi-tx-v1\n6\nfixture\n',bytes(range(256))*1024]:
            signature = crypto.device_signature(message,self.key)
            self.assertEqual(signature,expected.sign(message).signature)
            self.assertEqual(expected.verify_key.verify(message,signature),message)
        seeds = [crypto.device_seed() for _ in range(8)]
        self.assertEqual(len(set(seeds)),8)
        for seed in seeds:
            public = crypto.device_public(seed); signature = crypto.device_signature(b'new device',seed)
            self.assertEqual(VerifyKey(public).verify(b'new device',signature),b'new device')
        with self.assertRaises(ValueError):crypto.device_signature(b'fixture',self.key[:-1])
        for mode,content,aad,nonce,key in [(5,b'fixture',b'bad',b'\0'*24,self.key),
                (6,b'bad',b'',b'\0'*24,self.key),(7,b'',b'',b'\0'*24,self.key),
                (5,b'fixture',b'',self.nonce,self.key)]:
            request = struct.pack('!BBII',1,mode,len(content),len(aad))+key+nonce+aad+content
            result = subprocess.run([str(crypto.program())],input=request,capture_output=True,timeout=5)
            self.assertEqual(result.returncode,2);self.assertEqual(result.stdout,b'');self.assertEqual(result.stderr,b'')


if __name__ == '__main__':unittest.main()
