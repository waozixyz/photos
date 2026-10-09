"""Native ML-DSA interoperability with existing account identities."""
from concurrent.futures import ThreadPoolExecutor
import struct
import subprocess
import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pqcrypto.sign import ml_dsa_44
from photos import account_sign as signing
from photos.library import Library, atomic, packed
from photos.sync import Client


class AccountSignTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_public,cls.old_private = ml_dsa_44.generate_keypair()

    def test_existing_account_keys_sign_empty_binary_unicode_and_maximum_messages(self):
        for message in [b'',b'\0\xff\0', 'private 家族 📷'.encode(), b'x'*signing.LIMIT]:
            signature=signing.sign(self.old_private,message)
            self.assertEqual(len(signature),signing.SIGNATURE_BYTES)
            self.assertTrue(ml_dsa_44.verify(self.old_public,message,signature))
            self.assertTrue(signing.verify(self.old_public,message,signature))

    def test_new_native_accounts_work_with_existing_signatures_and_verifiers(self):
        accounts=[signing.generate_keypair() for _ in range(4)]
        self.assertEqual(len({public for public,_ in accounts}),4)
        message=b'daochi-sync-v1\nPOST\n/api/v1/sync/login\nfixture\n'
        for public,private in accounts:
            self.assertEqual(len(public),signing.PUBLIC_BYTES)
            self.assertEqual(len(private),signing.PRIVATE_BYTES)
            native=signing.sign(private,message)
            self.assertTrue(ml_dsa_44.verify(public,message,native))
            prior=ml_dsa_44.sign(private,message)
            self.assertTrue(signing.verify(public,message,prior))

    def test_parallel_signatures_and_altered_message_signature_or_public_fail_closed(self):
        message=b'daochi-tx-v1\n6\nprivate fixture\n'
        with ThreadPoolExecutor(max_workers=8) as pool:
            values=list(pool.map(lambda _:signing.sign(self.old_private,message),range(16)))
        for signature in values:
            self.assertTrue(ml_dsa_44.verify(self.old_public,message,signature))
        signature=values[0]
        altered=signature[:-1]+bytes([signature[-1]^1])
        public,_=signing.generate_keypair()
        for key,body,proof in [(self.old_public,message+b'changed',signature),
                               (self.old_public,message,altered),(public,message,signature)]:
            self.assertFalse(signing.verify(key,body,proof))

    def test_partial_headers_keys_payloads_oversized_and_appended_frames_emit_nothing(self):
        valid=struct.pack('!BBI',1,2,7)+self.old_private+b'fixture'
        cases=[b'',valid[:5],b'\0'+valid[1:],valid[:1]+b'\4'+valid[2:],valid[:-1],
               valid+b'extra',struct.pack('!BBI',1,1,1),struct.pack('!BBI',1,2,signing.LIMIT+1),
               struct.pack('!BBI',1,3,0)+self.old_public+b'partial signature']
        for data in cases:
            result=subprocess.run([str(signing.program())],input=data,capture_output=True,timeout=5)
            self.assertEqual(result.returncode,2)
            self.assertEqual(result.stdout,b'');self.assertEqual(result.stderr,b'')
        for private in [b'',self.old_private[:-1],self.old_private+b'extra']:
            with self.assertRaises(ValueError):signing.sign(private,b'fixture')
        with self.assertRaises(ValueError):signing.sign(self.old_private,b'x'*(signing.LIMIT+1))

    def test_unavailable_or_interrupted_native_signer_cannot_fall_back(self):
        for error in [OSError(),subprocess.TimeoutExpired('account signing',30)]:
            with patch.object(signing.subprocess,'run',side_effect=error):
                with self.assertRaisesRegex(ValueError,'preserved'):
                    signing.sign(self.old_private,b'fixture')

    def test_existing_identity_and_all_signed_routes_use_native_account_signing(self):
        with tempfile.TemporaryDirectory() as folder:
            library=Library(Path(folder)/'library')
            try:
                identity=library.root/'private/identity.json'
                atomic(identity,packed(dict(public=self.old_public.hex(),private=self.old_private.hex(),
                                           content_key='42'*32)).encode())
                before=identity.read_bytes()
                client=Client(library,dict(node='http://127.0.0.1:18080',node_id='a'*64))
                account=hashlib.sha256(self.old_public).hexdigest()
                responses=[dict(nonce='fixture-challenge'),dict(auth_token='fixture-token'),{}]
                with patch.object(client,'check_node'), patch.object(client,'raw',side_effect=responses) as raw:
                    client.login()
                login=raw.call_args_list[1]
                body=login.args[2]
                message='\n'.join(['daochi-sync-v1','POST','/api/v1/sync/login',
                                  hashlib.sha256(body).hexdigest(),'fixture-challenge','']).encode()
                self.assertTrue(ml_dsa_44.verify(self.old_public,message,bytes.fromhex(login.args[3]['X-Daochi-Signature'])))
                device=json.loads(raw.call_args_list[2].args[2])
                message=f'daochi-device-registration-v1\n{account}\nphotos\n{client.device_id}\n{client.client_id}\n{device["public_key"]}\n{device["nonce"]}\n{device["expires_at"]}\n'.encode()
                self.assertTrue(ml_dsa_44.verify(self.old_public,message,bytes.fromhex(device['signature'])))
                tx=json.loads(client.header('POST','/api/v1/sync',b'fixture'))
                message='\n'.join(['daochi-tx-v1','6',tx['tx_id'],account,'photos',client.device_id,
                                  'POST','/api/v1/sync',tx['body_sha256'],tx['nonce'],str(tx['expires_at']),'']).encode()
                self.assertTrue(ml_dsa_44.verify(self.old_public,message,bytes.fromhex(tx['signature'])))
                self.assertEqual(identity.read_bytes(),before)
            finally:library.db.close()

    def test_missing_signer_preserves_existing_identity_and_does_not_create_a_new_one(self):
        with tempfile.TemporaryDirectory() as folder:
            library=Library(Path(folder)/'library')
            try:
                identity=library.root/'private/identity.json'
                config=dict(node='http://127.0.0.1:18080',node_id='a'*64)
                with patch.object(signing,'program',side_effect=ValueError('unavailable')):
                    with self.assertRaises(ValueError):Client(library,config)
                self.assertFalse(identity.exists())
                atomic(identity,packed(dict(public=self.old_public.hex(),private=self.old_private.hex(),
                                           content_key='42'*32)).encode())
                before=identity.read_bytes();client=Client(library,config)
                device=client.device_path.read_bytes()
                from photos import sync_transaction
                with patch.object(sync_transaction,'program',side_effect=ValueError('unavailable')):
                    with self.assertRaises(ValueError):client.header('POST','/api/v1/sync',b'fixture')
                self.assertEqual(identity.read_bytes(),before)
                self.assertEqual(client.device_path.read_bytes(),device)
                self.assertEqual(library.query()['total'],0)
            finally:library.db.close()


if __name__ == '__main__':unittest.main()
