"""Canonical Mesh request preparation preserves existing Photos identities."""
import hashlib
import json
import struct
import subprocess
import unittest
from unittest.mock import patch

from nacl.signing import SigningKey
from pqcrypto.sign import ml_dsa_44

from photos import owner_account, sync_account, sync_transaction
from photos.sync import Client


class SyncAccountTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.public, cls.private = ml_dsa_44.generate_keypair()
        cls.seed = bytes(range(32))
        cls.client = 'photos-existing-device'
        cls.account = hashlib.sha256(cls.public).hexdigest()

    def test_login_matches_released_server_signature_context(self):
        nonce = 'ab' * 32
        body, signature = sync_account.login(self.public, self.private, self.client,
                                            json.dumps(dict(nonce=nonce)).encode())
        self.assertEqual(json.loads(body), dict(user_id_hash=self.account, client_id=self.client,
                                               public_key=self.public.hex()))
        message = ('daochi-sync-v1\nPOST\n/api/v1/sync/login\n'
                   + hashlib.sha256(body).hexdigest() + '\n' + nonce + '\n').encode()
        self.assertTrue(ml_dsa_44.verify(self.public, message, bytes.fromhex(signature)))
        self.assertFalse(ml_dsa_44.verify(self.public, message + b'changed', bytes.fromhex(signature)))

    def test_registration_preserves_device_and_verifies_canonical_message(self):
        before = self.public, self.private, self.seed
        records = [json.loads(sync_account.registration(*before, self.client, 2000000000))
                   for _ in range(3)]
        public = bytes(SigningKey(self.seed).verify_key)
        self.assertEqual(len({r['nonce'] for r in records}), 3)
        for record in records:
            self.assertEqual(record['app_id'], 'photos')
            self.assertEqual(record['public_key'], public.hex())
            self.assertEqual(record['device_key_id'], hashlib.sha256(public).hexdigest())
            self.assertEqual(record['client_id'], self.client)
            self.assertEqual(record['expires_at'], 2000000300)
            message = '\n'.join(['daochi-device-registration-v1', self.account, 'photos',
                                 record['device_key_id'], self.client, record['public_key'],
                                 record['nonce'], str(record['expires_at']), '']).encode()
            self.assertTrue(ml_dsa_44.verify(self.public, message, bytes.fromhex(record['signature'])))
        self.assertEqual(before, (self.public, self.private, self.seed))

    def test_invalid_challenge_and_mismatched_existing_keys_emit_nothing(self):
        _, other_private = ml_dsa_44.generate_keypair()
        for challenge, private in ((b'{}', self.private), (b'{"nonce":"wrong"}', self.private),
                                   (json.dumps(dict(nonce='ab' * 32)).encode(), other_private)):
            with self.subTest(challenge=challenge):
                with self.assertRaisesRegex(ValueError, 'preserved'):
                    sync_account.login(self.public, private, self.client, challenge)

    def test_partial_and_trailing_private_frames_emit_nothing(self):
        client = self.client.encode()
        challenge = json.dumps(dict(nonce='ab' * 32)).encode()
        frame = (struct.pack('!BBHIQ', 2, 1, len(client), len(challenge), 0)
                 + self.public + self.private + b'\0' * 32 + client + challenge)
        for value in (frame[:12], frame[:-1], frame + b'extra', frame[:1] + b'\3' + frame[2:]):
            result = subprocess.run([str(sync_transaction.program())], input=value,
                                    capture_output=True, timeout=5)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (2, b'', b''))

    def test_interrupted_builder_and_malformed_response_cannot_fall_back(self):
        with patch.object(sync_transaction.subprocess, 'run', side_effect=OSError()):
            with self.assertRaisesRegex(ValueError, 'preserved'):
                sync_account.login(self.public, self.private, self.client, b'{}')
        for value in (b'', b'\0' * 8, struct.pack('!II', 1, 4840) + b'{'):
            with patch.object(sync_transaction, 'run', return_value=value):
                with self.assertRaisesRegex(ValueError, 'Invalid Mesh account response'):
                    sync_account.login(self.public, self.private, self.client, b'{}')

    def test_failed_device_registration_does_not_publish_a_login_token(self):
        client = Client.__new__(Client)
        client.owner, client.device_key = owner_account.Account(self.public, self.private), self.seed
        client.client_id, client.tokens, client.legacy = self.client, {}, None
        client.node = client.legacy_node = 'http://127.0.0.1:18080'
        with patch.object(client, 'check_node') as check, patch.object(client, 'raw', side_effect=[
                dict(nonce='ab' * 32), dict(auth_token='synthetic-token'), ValueError('registration failed')]):
            with self.assertRaisesRegex(ValueError, 'registration failed'):
                client.login()
        check.assert_called_once()
        self.assertEqual(client.tokens, {})
