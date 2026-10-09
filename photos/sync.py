"""Encrypted Daochi v6 sync, exclusively to the pinned ThinkPad node."""
import base64
import hashlib
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
from urllib.parse import urlsplit
from datetime import datetime, timezone

from .sync_crypto import encrypt, decrypt, seal_blob, seal_record, device_seed, device_public, device_signature
from . import account_sign, sync_http

from .library import atomic, packed, digest_file

APP = 'photos'
COLLECTION = 'private.photos.v1.records.media'
ALBUM_COLLECTION = 'private.photos.v1.records.albums'
CHUNK = 8 * 1024 * 1024
RECORD_PAGE = 200
HASH = re.compile(r'^[0-9a-f]{64}$')


def encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def decode(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


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
        self.identity_path = Path(identity_path or library.root / 'private' / 'identity.json')
        if not self.identity_path.exists():
            public, private = account_sign.generate_keypair()
            atomic(self.identity_path, packed(dict(public=public.hex(), private=private.hex(),
                                                 content_key=secrets.token_hex(32))).encode())
        self.identity = json.loads(self.identity_path.read_bytes())
        self.public = bytes.fromhex(self.identity['public'])
        self.private = bytes.fromhex(self.identity['private'])
        self.account = hashlib.sha256(self.public).hexdigest()
        self.key = bytes.fromhex(self.identity['content_key'])
        if (len(self.public) != account_sign.PUBLIC_BYTES or
                len(self.private) != account_sign.PRIVATE_BYTES or len(self.key) != 32):
            raise ValueError('Invalid Photos account identity; existing keys were preserved')
        self.device_path = self.identity_path.with_name('device.json')
        if not self.device_path.exists():
            atomic(self.device_path, packed(dict(key=device_seed().hex(), client='photos-' + secrets.token_hex(12))).encode())
        device = json.loads(self.device_path.read_bytes())
        self.device_key = bytes.fromhex(device['key'])
        self.device_public = device_public(self.device_key)
        self.device_id = hashlib.sha256(self.device_public).hexdigest()
        self.client_id = device['client']
        self.token = None
        self.auth_lock = threading.Lock()
        self.checked = False

    def raw(self, path, method='GET', body=None, headers=None, binary=False, limit=128 * 1024 * 1024):
        data = sync_http.request(self.node,path,method,body,headers,limit)
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
        signature = account_sign.sign(self.private, message).hex()
        result = self.raw('/api/v1/sync/login', 'POST', body,
                          {'Content-Type': 'application/json', 'X-Daochi-User': self.account, 'X-Daochi-Signature': signature})
        self.token = result['auth_token']
        public = self.device_public.hex()
        nonce, expires = secrets.token_hex(32), int(time.time()) + 120
        message = f'daochi-device-registration-v1\n{self.account}\n{APP}\n{self.device_id}\n{self.client_id}\n{public}\n{nonce}\n{expires}\n'.encode()
        body = packed(dict(app_id=APP, device_key_id=self.device_id, client_id=self.client_id,
                           public_key=public, nonce=nonce, expires_at=expires,
                           signature=account_sign.sign(self.private, message).hex())).encode()
        self.raw('/api/v1/account/devices', 'POST', body,
                 {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.token})

    def header(self, method, path, body):
        value = dict(protocol_version=6, tx_id=secrets.token_hex(32), account_id=self.account,
                     app_id=APP, device_key_id=self.device_id, method=method, path=path,
                     body_sha256=hashlib.sha256(body or b'').hexdigest(), nonce=secrets.token_hex(32),
                     expires_at=int(time.time()) + 120, signature_context='daochi-tx-v1')
        message = '\n'.join(['daochi-tx-v1', '6', value['tx_id'], self.account, APP, self.device_id,
                             method, path, value['body_sha256'], value['nonce'], str(value['expires_at']), '']).encode()
        value['signature'] = account_sign.sign(self.private, message).hex()
        value['device_signature'] = device_signature(message, self.device_key).hex()
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
        data = seal_blob(plain, self.aad(photo_id, part), self.key)
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
        from .semantic import encrypted_index
        photo['semantic'] = encrypted_index(self.library, photo['id'])
        from .people import export_index
        photo['people'] = export_index(self.library,photo['id'])
        return self.seal_record(photo, COLLECTION, 'photos.media.v1')

    def album_record(self, album):
        album = dict(album)
        album.pop('dirty', None)
        album['auto_people'] = json.loads(album['auto_people'])
        return self.seal_record(album, ALBUM_COLLECTION, 'photos.album.v1')

    def seal_record(self, value, collection, schema):
        aad = dict(account_id=self.account, app_id=APP, collection=collection, id=value['id'], schema=schema)
        sealed = seal_record(packed(value).encode(), packed(aad).encode(), self.key)
        nonce, cipher = sealed[:24], sealed[24:]
        envelope = dict(profile='daochi-record-v1', alg='xchacha20poly1305', key_id='main',
                        nonce=encode(nonce), aad=aad, ciphertext=encode(cipher))
        return dict(collection=collection, id=value['id'], key_id='main', nonce=encode(nonce),
                    ciphertext=encode(packed(envelope).encode()), schema_version=1,
                    updated_at=datetime.fromtimestamp(value['updated'], timezone.utc).isoformat().replace('+00:00', 'Z'))

    def accept_album(self, record):
        envelope = json.loads(decode(record['ciphertext']))
        aad = dict(account_id=self.account, app_id=APP, collection=ALBUM_COLLECTION, id=record['id'], schema='photos.album.v1')
        if envelope.get('aad') != aad or envelope.get('nonce') != record.get('nonce') or envelope.get('key_id') != record.get('key_id') or envelope.get('alg') != 'xchacha20poly1305':
            raise ValueError('Album metadata was moved or altered')
        album = json.loads(decrypt(decode(envelope['ciphertext']), packed(aad).encode(), decode(envelope['nonce']), self.key))
        if album.get('id') != record['id'] or self.library.album_name(album.get('name')) != album.get('name'):
            raise ValueError('Invalid album record')
        if not isinstance(album.get('description'), str) or len(album['description'].encode()) > 8192 or not isinstance(album.get('cover'), str) or album['cover'] and not HASH.fullmatch(album['cover']):
            raise ValueError('Invalid album details')
        album.setdefault('filter', '')
        if not isinstance(album['filter'], str) or len(album['filter'].encode()) > 512:
            raise ValueError('Invalid smart album search')
        if album['filter']:
            from .query import validate
            validate(self.library, album['filter'])
        if any(type(album.get(k)) not in {bool, int} or album[k] not in (0, 1) for k in ['pinned', 'archived', 'deleted']):
            raise ValueError('Invalid album state')
        if any(type(album.get(k)) not in {int, float} or not 0 <= album[k] < 32503680000 for k in ['created', 'updated']):
            raise ValueError('Invalid album date')
        existing = self.library.db.execute('SELECT * FROM albums WHERE id=?', (album['id'],)).fetchone()
        from .auto_albums import identities
        selected = identities(album.get('auto_people',json.loads(existing['auto_people']) if existing else []))
        if album['filter'] and selected:
            raise ValueError('Smart albums cannot auto-add people')
        album['auto_people'] = packed(selected)
        if existing and existing['updated'] >= album['updated']:
            return
        other = self.library.db.execute('SELECT * FROM albums WHERE name=? AND id<>?', (album['name'], album['id'])).fetchone()
        if other:
            # Legacy photo records may have reconstructed a zero-version name.
            # A real conflicting edit must remain queued for explicit resolution.
            if other['updated'] != 0:
                raise ValueError('Album name conflict; local edits are retained')
            self.library.db.execute('DELETE FROM albums WHERE id=?', (other['id'],))
        columns = ['id', 'name', 'description', 'cover', 'pinned', 'archived', 'deleted', 'created', 'updated', 'filter','auto_people']
        self.library.db.execute('INSERT OR REPLACE INTO albums(' + ','.join(columns) + ',dirty) VALUES(' + ','.join('?' for _ in columns) + ',0)', [album[k] for k in columns])

    def accept(self, record):
        if record.get('collection') == ALBUM_COLLECTION:
            self.accept_album(record)
            return
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
        if not isinstance(photo['albums'], list) or not isinstance(photo['manifest'], dict) or type(photo['taken']) not in {int, float} or not (-62135596800 <= photo['taken'] < 253402300800):
            raise ValueError('Invalid photo metadata')
        photo.setdefault('edits', {})
        photo.setdefault('archived', 0)
        if type(photo['archived']) not in {int, bool} or photo['archived'] not in (0, 1):
            raise ValueError('Invalid photo archive state')
        if not isinstance(photo['edits'], dict):
            raise ValueError('Invalid photo edits')
        if 'auto_albums' in photo['edits']:
            from .auto_albums import validate_provenance
            photo['edits']['auto_albums'] = validate_provenance(photo['edits']['auto_albums'])
        existing = self.library.db.execute('SELECT * FROM photos WHERE id=?', (photo['id'],)).fetchone()
        if existing and existing['updated'] >= photo['updated']:
            return
        if 'semantic' in photo:
            from .semantic import accept_index
            accept_index(self.library, photo['id'], photo['semantic'])
        if 'people' in photo:
            from .people import accept_index
            accept_index(self.library,photo['id'],photo['people'])
        columns = ['id', 'name', 'extension', 'kind', 'size', 'taken', 'width', 'height', 'metadata', 'albums',
                   'favorite', 'hidden', 'trashed', 'archived', 'caption', 'tags', 'updated', 'manifest', 'edits']
        for key in ['metadata', 'albums', 'manifest', 'edits']:
            photo[key] = packed(photo[key])
        self.library.db.execute('INSERT OR REPLACE INTO photos(' + ','.join(columns) + ',dirty) VALUES(' + ','.join('?' for _ in columns) + ',0)',
                                [photo[c] for c in columns])
        for name in json.loads(photo['albums']):
            self.library._ensure_album(name, dirty=0)

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
                    if not isinstance(record, dict) or record.get('collection') not in {COLLECTION, ALBUM_COLLECTION} or not HASH.fullmatch(record.get('id', '')):
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
        album_rows = self.library.db.execute('SELECT * FROM albums WHERE dirty=1 ORDER BY updated LIMIT ?', (maximum,)).fetchall()
        records = [self.album_record(row) for row in album_rows]
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
            # Face-rich photos carry larger encrypted records. Split by byte
            # budget as well as record count to fit the pinned node's request
            # limit; failed batches retain every dirty marker for safe retry.
            batches, batch, size = [], [], 0
            for record in records:
                length = len(packed(record).encode()) + 1
                if length>750000: raise ValueError('Photo metadata exceeds the encrypted sync page; it remains queued')
                if batch and size+length>750000:
                    batches.append(batch); batch, size = [], 0
                batch.append(record); size += length
            if batch: batches.append(batch)
            for batch in batches:
                result = self.exchange(batch)
                if result.get('full_snapshot_required') or result.get('status') not in {'ok', 'success', 'synced'}:
                    raise ValueError('Node did not confirm the upload; it remains queued')
            for row in rows:
                self.library.db.execute('UPDATE photos SET dirty=0 WHERE id=? AND updated=?', (row['id'], row['updated']))
            for row in album_rows:
                self.library.db.execute('UPDATE albums SET dirty=0 WHERE id=? AND updated=?', (row['id'], row['updated']))
            self.library.db.commit()
        pending = self.library.db.execute('SELECT COUNT(*) FROM photos WHERE dirty=1').fetchone()[0]
        pending += self.library.db.execute('SELECT COUNT(*) FROM albums WHERE dirty=1').fetchone()[0]
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
        chunks = manifest.get('chunks')
        if not isinstance(chunks, list) or (not chunks and photo['size'] != 0):
            raise ValueError('This original has not finished syncing yet')
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix='.download-', dir=destination.parent)
        try:
            value = hashlib.sha256()
            size = 0
            with os.fdopen(fd, 'wb') as output:
                for index, digest in enumerate(chunks):
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
