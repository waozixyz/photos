"""Encrypted Daochi v6 sync, exclusively to the pinned ThinkPad node."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timezone

from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_encrypt as encrypt
from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_decrypt as decrypt
from nacl.signing import SigningKey
from pqcrypto.sign import ml_dsa_44

from .library import atomic, packed, digest_file

APP = 'photos'
COLLECTION = 'private.photos.v1.records.media'
CHUNK = 8 * 1024 * 1024
RECORD_PAGE = 200
HASH = re.compile(r'^[0-9a-f]{64}$')


def encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def decode(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Photos forbids redirects outside its pinned local node')


class Client:
    def __init__(self, library, config, identity_path=None):
        self.library = library
        self.config = config
        origin = urlsplit(config['node'])
        if origin.scheme != 'http' or origin.hostname != '127.0.0.1' or not origin.port or origin.path or origin.query or origin.fragment or origin.username:
            raise ValueError('Photos sync requires loopback to ThinkPad; remote URLs are forbidden')
        if not HASH.fullmatch(config.get('node_id', '')):
            raise ValueError('The ThinkPad node identity must be pinned')
        self.node = config['node'].rstrip('/')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.identity_path = Path(identity_path or library.root / 'private' / 'identity.json')
        if not self.identity_path.exists():
            public, private = ml_dsa_44.generate_keypair()
            atomic(self.identity_path, packed(dict(public=public.hex(), private=private.hex(),
                                                 content_key=secrets.token_hex(32))).encode())
        self.identity = json.loads(self.identity_path.read_bytes())
        self.public = bytes.fromhex(self.identity['public'])
        self.private = bytes.fromhex(self.identity['private'])
        self.account = hashlib.sha256(self.public).hexdigest()
        self.key = bytes.fromhex(self.identity['content_key'])
        self.device_path = self.identity_path.with_name('device.json')
        if not self.device_path.exists():
            atomic(self.device_path, packed(dict(key=bytes(SigningKey.generate()).hex(), client='photos-' + secrets.token_hex(12))).encode())
        device = json.loads(self.device_path.read_bytes())
        self.device = SigningKey(bytes.fromhex(device['key']))
        self.device_id = hashlib.sha256(bytes(self.device.verify_key)).hexdigest()
        self.client_id = device['client']
        self.token = None
        self.auth_lock = threading.Lock()
        self.checked = False

    def raw(self, path, method='GET', body=None, headers=None, binary=False, limit=128 * 1024 * 1024):
        if not path.startswith('/api/v1/') or '?' in path and not path.startswith('/api/v1/sync/challenge?'):
            raise ValueError('Invalid local sync route')
        request = urllib.request.Request(self.node + path, data=body, method=method, headers=headers or {})
        with self.opener.open(request, timeout=60) as response:
            data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError('Local node response exceeded its safe limit')
            if binary:
                return data
            return json.loads(data) if data else {}

    def check_node(self):
        value = self.raw('/api/v1/node')
        if value.get('node_id') != self.config['node_id']:
            raise ValueError('Node identity mismatch: Photos refuses to sync')
        for peer in value.get('known_nodes', []):
            policy = peer.get('sync') or {}
            direction = policy.get('direction', '')
            scopes = policy.get('apps', [])
            if direction != 'pull' and (not scopes or APP in scopes or '*' in scopes):
                raise ValueError('Node has outward Photos replication; local-only sync refused')
        self.checked = True

    def login(self):
        self.check_node()
        challenge = self.raw('/api/v1/sync/challenge?user_id=' + self.account)
        body = packed(dict(user_id_hash=self.account, client_id=self.client_id, public_key=self.public.hex())).encode()
        message = '\n'.join(['daochi-sync-v1', 'POST', '/api/v1/sync/login',
                             hashlib.sha256(body).hexdigest(), challenge['nonce'], '']).encode()
        signature = ml_dsa_44.sign(self.private, message).hex()
        result = self.raw('/api/v1/sync/login', 'POST', body,
                          {'Content-Type': 'application/json', 'X-Daochi-User': self.account, 'X-Daochi-Signature': signature})
        self.token = result['auth_token']
        public = bytes(self.device.verify_key).hex()
        nonce, expires = secrets.token_hex(32), int(time.time()) + 120
        message = f'daochi-device-registration-v1\n{self.account}\n{APP}\n{self.device_id}\n{self.client_id}\n{public}\n{nonce}\n{expires}\n'.encode()
        body = packed(dict(app_id=APP, device_key_id=self.device_id, client_id=self.client_id,
                           public_key=public, nonce=nonce, expires_at=expires,
                           signature=ml_dsa_44.sign(self.private, message).hex())).encode()
        self.raw('/api/v1/account/devices', 'POST', body,
                 {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.token})

    def header(self, method, path, body):
        value = dict(protocol_version=6, tx_id=secrets.token_hex(32), account_id=self.account,
                     app_id=APP, device_key_id=self.device_id, method=method, path=path,
                     body_sha256=hashlib.sha256(body or b'').hexdigest(), nonce=secrets.token_hex(32),
                     expires_at=int(time.time()) + 120, signature_context='daochi-tx-v1')
        message = '\n'.join(['daochi-tx-v1', '6', value['tx_id'], self.account, APP, self.device_id,
                             method, path, value['body_sha256'], value['nonce'], str(value['expires_at']), '']).encode()
        value['signature'] = ml_dsa_44.sign(self.private, message).hex()
        value['device_signature'] = self.device.sign(message).signature.hex()
        return packed(value)

    def request(self, path, method='GET', body=None, binary=False):
        for attempt in range(2):
            if not self.token:
                with self.auth_lock:
                    if not self.token:
                        self.login()
            token = self.token
            headers = {'Authorization': 'Bearer ' + token,
                       'X-Daochi-User': self.account, 'X-Daochi-Tx': self.header(method, path, body),
                       'Content-Type': 'application/octet-stream' if binary else 'application/json'}
            try:
                return self.raw(path, method, body, headers, binary)
            except urllib.error.HTTPError as error:
                if error.code != 401 or attempt:
                    raise
                with self.auth_lock:
                    if self.token == token:
                        self.token = None
                        self.login()
        raise ValueError('Local node authentication failed')

    def aad(self, photo_id, part):
        return packed(dict(account_id=self.account, app_id=APP, id=photo_id,
                           schema='photos.media.v1', part=part)).encode()

    def upload_blob(self, plain, photo_id, part):
        # Bind the nonce to the actual chunk bytes as well as their address.
        # Even a modified local original cannot reuse a nonce for other bytes.
        # Identical retries remain resumable; metadata uses random nonces.
        nonce_input = self.aad(photo_id, part) + hashlib.sha256(plain).digest()
        nonce = hmac.new(self.key, nonce_input, hashlib.sha256).digest()[:24]
        data = nonce + encrypt(plain, self.aad(photo_id, part), nonce, self.key)
        digest = hashlib.sha256(data).hexdigest()
        path = f'/api/v1/blobs/{APP}/{digest}'
        try:
            self.request(path, 'HEAD', binary=True)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            self.request(path, 'PUT', data, binary=True)
        return digest

    def fetch_blob(self, digest, photo_id, part):
        if not HASH.fullmatch(digest):
            raise ValueError('Invalid encrypted blob reference')
        data = self.request(f'/api/v1/blobs/{APP}/{digest}', binary=True)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Encrypted original failed its integrity check')
        return decrypt(data[24:], self.aad(photo_id, part), data[:24], self.key)

    def prepare_original(self, photo):
        manifest = json.loads(photo['manifest'])
        if manifest.get('chunks'):
            return manifest
        source = self.library.original(photo)
        if not source.exists():
            raise ValueError('Original is missing; sync will retry without marking it complete')
        chunks = []
        whole = hashlib.sha256()
        with source.open('rb') as incoming:
            for index, block in enumerate(iter(lambda: incoming.read(CHUNK), b'')):
                whole.update(block)
                chunks.append(self.upload_blob(block, photo['id'], f'chunk:{index}'))
        if whole.hexdigest() != photo['id']:
            raise ValueError('Local original changed; refusing to sync corrupted bytes')
        manifest = dict(chunks=chunks, chunk_bytes=CHUNK)
        thumbnail = self.library.thumbnail(photo['id'])
        if thumbnail.exists():
            # A preview is content-addressed too, so regenerated previews never
            # reuse an encryption nonce with changed bytes.
            data = thumbnail.read_bytes()
            preview_id = hashlib.sha256(data).hexdigest()
            manifest['preview'] = self.upload_blob(data, preview_id, 'preview')
            manifest['preview_id'] = preview_id
        return manifest

    def save_manifest(self, photo, manifest):
        self.library.db.execute('UPDATE photos SET manifest=? WHERE id=?', (packed(manifest), photo['id']))
        self.library.db.commit()

    def upload_original(self, photo):
        manifest = self.prepare_original(photo)
        self.save_manifest(photo, manifest)
        return manifest

    def record(self, photo, manifest=None):
        photo = dict(photo)
        photo['manifest'] = self.upload_original(photo) if manifest is None else manifest
        photo['albums'] = json.loads(photo['albums'])
        photo['metadata'] = json.loads(photo['metadata'])
        photo['edits'] = json.loads(photo['edits'])
        photo.pop('dirty', None)
        nonce = secrets.token_bytes(24)
        aad = dict(account_id=self.account, app_id=APP, collection=COLLECTION, id=photo['id'], schema='photos.media.v1')
        cipher = encrypt(packed(photo).encode(), packed(aad).encode(), nonce, self.key)
        envelope = dict(profile='daochi-record-v1', alg='xchacha20poly1305', key_id='main',
                        nonce=encode(nonce), aad=aad, ciphertext=encode(cipher))
        return dict(collection=COLLECTION, id=photo['id'], key_id='main', nonce=encode(nonce),
                    ciphertext=encode(packed(envelope).encode()), schema_version=1,
                    updated_at=datetime.fromtimestamp(photo['updated'], timezone.utc).isoformat().replace('+00:00', 'Z'))

    def accept(self, record):
        if record.get('collection') != COLLECTION or not HASH.fullmatch(record.get('id', '')):
            return
        if record.get('deleted_at'):
            # The app retains originals in Trash, never permanently deletes them.
            return
        envelope = json.loads(decode(record['ciphertext']))
        aad = dict(account_id=self.account, app_id=APP, collection=COLLECTION, id=record['id'], schema='photos.media.v1')
        if envelope.get('aad') != aad or envelope.get('nonce') != record.get('nonce') or envelope.get('key_id') != record.get('key_id') or envelope.get('alg') != 'xchacha20poly1305':
            raise ValueError('Photo record metadata was moved or altered')
        photo = json.loads(decrypt(decode(envelope['ciphertext']), packed(aad).encode(), decode(envelope['nonce']), self.key))
        if photo.get('id') != record['id'] or photo.get('extension') not in __import__('photos.library', fromlist=['IMAGES']).IMAGES | __import__('photos.library', fromlist=['VIDEOS']).VIDEOS:
            raise ValueError('Invalid photo record')
        if not isinstance(photo['albums'], list) or not isinstance(photo['manifest'], dict) or not (0 < photo['taken'] < 32503680000):
            raise ValueError('Invalid photo metadata')
        photo.setdefault('edits', {})
        if not isinstance(photo['edits'], dict):
            raise ValueError('Invalid photo edits')
        existing = self.library.db.execute('SELECT * FROM photos WHERE id=?', (photo['id'],)).fetchone()
        if existing and existing['updated'] >= photo['updated']:
            return
        columns = ['id', 'name', 'extension', 'kind', 'size', 'taken', 'width', 'height', 'metadata', 'albums',
                   'favorite', 'hidden', 'trashed', 'caption', 'tags', 'updated', 'manifest', 'edits']
        for key in ['metadata', 'albums', 'manifest', 'edits']:
            photo[key] = packed(photo[key])
        self.library.db.execute('INSERT OR REPLACE INTO photos(' + ','.join(columns) + ',dirty) VALUES(' + ','.join('?' for _ in columns) + ',0)',
                                [photo[c] for c in columns])

    def exchange(self, records=None):
        uploaded = None
        while True:
            since = self.library.setting('server_version', 0)
            cursor = self.library.setting('record_cursor', None)
            request = dict(protocol_version=6, app_id=APP, user_id_hash=self.account, client_id=self.client_id,
                           client_clock=since, since_server_version=since,
                           encrypted_records_only=True, record_limit=RECORD_PAGE, encrypted_records=records or [])
            if cursor:
                request['record_cursor'] = cursor
            result = self.request('/api/v1/sync', 'POST', packed(request).encode())
            if result.get('encrypted_records_only') is not True or result.get('status') != 'ok' or result.get('full_snapshot_required'):
                raise ValueError('ThinkPad node needs paged encrypted sync; changes remain queued')
            version = result.get('server_version')
            if type(version) is not int or version < since:
                raise ValueError('Node returned an invalid metadata version; the cursor was retained')
            incoming = result.get('changes', {}).get('encrypted_records') or []
            if not isinstance(incoming, list) or len(incoming) > RECORD_PAGE:
                raise ValueError('Node returned an invalid metadata page')
            complete = result.get('changes_complete')
            following = result.get('record_cursor')
            if complete is not True:
                valid = (complete is False and isinstance(following, dict) and
                         type(following.get('version')) is int and since < following['version'] <= version and
                         isinstance(following.get('collection'), str) and isinstance(following.get('id'), str) and
                         bool(incoming))
                previous_key = (cursor['version'], cursor['collection'], cursor['id']) if cursor else (since, '', '')
                next_key = (following['version'], following['collection'], following['id']) if valid else previous_key
                if not valid or next_key <= previous_key:
                    raise ValueError('Node returned a stalled metadata page; the cursor was retained')
                last = incoming[-1]
                if (following['collection'], following['id']) != (last.get('collection'), last.get('id')):
                    raise ValueError('Node returned an invalid page boundary; the cursor was retained')
            elif following is not None:
                raise ValueError('Node returned a conflicting metadata cursor')
            # Keep decrypted records and their checkpoint in one transaction.
            # A failed decrypt or write must never acknowledge a partial page.
            try:
                self.library.db.execute('BEGIN IMMEDIATE')
                for record in incoming:
                    if not isinstance(record, dict) or record.get('collection') != COLLECTION or not HASH.fullmatch(record.get('id', '')):
                        raise ValueError('Node returned an unsupported photo record; the cursor was retained')
                    self.accept(record)
                for key, value in [('server_version', version if complete else since),
                                   ('record_cursor', None if complete else following)]:
                    self.library.db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', (key, packed(value)))
                self.library.db.commit()
            except Exception:
                self.library.db.rollback()
                raise
            uploaded = uploaded or result
            records = None
            if complete:
                return uploaded

    def sync(self, maximum=50):
        self.check_node()
        self.exchange()
        rows = self.library.db.execute('SELECT * FROM photos WHERE dirty=1 ORDER BY updated LIMIT ?', (maximum,)).fetchall()
        records = []
        if rows:
            self.library.set_setting('status', 'Syncing originals to ThinkPad · local only')
            # Workers handle immutable files and encrypted HTTP only. All SQLite
            # access and metadata edits remain on the owning thread.
            with ThreadPoolExecutor(max_workers=4) as uploads:
                futures = {uploads.submit(self.prepare_original, dict(row)): dict(row) for row in rows}
                for future in as_completed(futures):
                    photo = futures[future]
                    manifest = future.result()
                    self.save_manifest(photo, manifest)
                    records.append(self.record(photo, manifest))
        if records:
            result = self.exchange(records)
            if result.get('full_snapshot_required') or result.get('status') not in {'ok', 'success', 'synced'}:
                raise ValueError('Node did not confirm the upload; it remains queued')
            for row in rows:
                self.library.db.execute('UPDATE photos SET dirty=0 WHERE id=? AND updated=?', (row['id'], row['updated']))
            self.library.db.commit()
        pending = self.library.db.execute('SELECT COUNT(*) FROM photos WHERE dirty=1').fetchone()[0]
        self.library.set_setting('status', f'ThinkPad sync · {pending:,} queued' if pending else 'All photos synced · ThinkPad only')
        return pending

    def materialize(self, photo_id, preview_only=False):
        photo = self.library.photo(photo_id)
        manifest = json.loads(photo['manifest'])
        thumbnail = self.library.thumbnail(photo_id)
        if not thumbnail.exists() and manifest.get('preview'):
            atomic(thumbnail, self.fetch_blob(manifest['preview'], manifest['preview_id'], 'preview'))
        if preview_only:
            return str(thumbnail) if thumbnail.exists() else ''
        destination = self.library.original(photo)
        if destination.exists():
            return str(destination)
        if not manifest.get('chunks'):
            raise ValueError('This original has not finished syncing yet')
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix='.download-', dir=destination.parent)
        try:
            value = hashlib.sha256()
            size = 0
            with os.fdopen(fd, 'wb') as output:
                for index, digest in enumerate(manifest['chunks']):
                    block = self.fetch_blob(digest, photo_id, f'chunk:{index}')
                    value.update(block)
                    size += len(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if value.hexdigest() != photo_id or size != photo['size']:
                raise ValueError('Original download failed integrity verification')
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
        self.library.preview(photo, destination)
        return str(destination)
