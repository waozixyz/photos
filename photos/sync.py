"""Encrypted Daochi v6 sync, exclusively to pinned private nodes on loopback.

Photos and albums are the account's shared entity data (Daochi
docs/entity-data.md): one record and one set of encrypted chunks per photo,
which Diary and every other app of the account show too. The account is the
owner's one Daochi account, the one Inbe signed in with. Photos keeps only
what it derives itself (file metadata, album bookkeeping, search and face
indexes) in its own private annotations, sealed with the account's photo
record key and bound to Photos' collection, so every Photos install of the
account reads them.

The entity data lives on the pinned `node`. A library synced before entity
data still has its copies in the account Photos used to have, on the pinned
`legacy_node` (the same node when none is set), which Photos only reads from
while moving them. Each photo moves on its own; its old records stay
readable for older Photos releases through 2027-10-24. Its old chunks are
deleted only when the configuration allows it (`retire_legacy_chunks`), after
the node confirmed the new record and the new chunks were downloaded again
and checked against the photo's hash.
"""
import base64
import hashlib
import json
import math
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
from datetime import datetime, timedelta, timezone

from .sync_crypto import decrypt, seal_record, device_seed, device_public
from . import entity_crypto, owner_account, sync_http, sync_pages, sync_transaction, sync_account

from .library import atomic, packed, IMAGES, VIDEOS

APP = 'photos'
ENTITY = 'private.entity.v1.'
PHOTOS = ENTITY + 'photos'
ALBUMS = ENTITY + 'albums'
ANNOTATIONS = 'private.photos.v1.records.annotations'
# The earlier account's collections, read through 2027-10-24.
LEGACY_MEDIA = 'private.photos.v1.records.media'
LEGACY_ALBUMS = 'private.photos.v1.records.albums'
# Photos' own bookkeeping in a photo's edits, kept with its annotation.
LOCAL_EDITS = ('auto_albums', 'removed_albums', 'live_separated')
# What the person says about a photo; another app may not know every field.
PERSON = ('name', 'trashed', 'favorite', 'hidden', 'archived', 'caption', 'tags', 'edits')
CHUNK = entity_crypto.CHUNK
RECORD_PAGE = 200
# Daochi takes at most this many records in one signed sync request.
UPLOAD_RECORDS = 200
HASH = re.compile(r'^[0-9a-f]{64}$')
ALBUM_ID = re.compile(r'^[0-9a-f]{32}$')
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
TYPES = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.jpe': 'image/jpeg', '.png': 'image/png',
         '.webp': 'image/webp', '.gif': 'image/gif', '.heic': 'image/heic', '.heif': 'image/heif',
         '.hif': 'image/heif', '.avif': 'image/avif', '.tif': 'image/tiff', '.tiff': 'image/tiff',
         '.bmp': 'image/bmp', '.dng': 'image/x-adobe-dng', '.mp4': 'video/mp4', '.m4v': 'video/x-m4v',
         '.mov': 'video/quicktime', '.3gp': 'video/3gpp', '.avi': 'video/x-msvideo', '.webm': 'video/webm',
         '.mpg': 'video/mpeg', '.mpeg': 'video/mpeg', '.mkv': 'video/x-matroska', '.wmv': 'video/x-ms-wmv',
         '.flv': 'video/x-flv'}
EXTENSIONS = {'image/jpeg': '.jpg', 'image/heif': '.heif', 'image/tiff': '.tif', 'video/mpeg': '.mpg',
              **{kind: extension for extension, kind in TYPES.items()
                 if kind not in {'image/jpeg', 'image/heif', 'image/tiff', 'video/mpeg'}}}


def encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def decode(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def stamp(value):
    # Whole microseconds, never rounded up past the local edit time.
    return (EPOCH + timedelta(microseconds=math.floor(value * 1000000))).isoformat().replace('+00:00', 'Z')


def media_type(photo):
    extension = photo['extension']
    if extension in TYPES:
        return TYPES[extension]
    return ('video/' if photo['kind'] == 'video' else 'image/') + extension.lstrip('.')


def tag_list(text):
    return [tag.strip() for tag in text.split(',') if tag.strip()]


def blob_list(value):
    if not isinstance(value, list) or len(value) > 1000000 or any(
            not isinstance(blob, dict) or not isinstance(blob.get('address'), str) or not HASH.fullmatch(blob['address'])
            or type(blob.get('size')) is not int or not 16 < blob['size'] <= CHUNK + 16 for blob in value):
        raise ValueError('Invalid shared photo chunks')
    return value


def exact(value):
    # Whole seconds as integers, as other apps write them; fractions are kept.
    return int(value) if float(value).is_integer() else value


def number(value, low=0, high=32503680000):
    if type(value) not in {int, float} or not low <= value < high:
        raise ValueError('Invalid shared photo time')
    return value


def pinned(config, node, pin):
    origin = urlsplit(config[node])
    if origin.scheme != 'http' or origin.hostname != '127.0.0.1' or not origin.port or origin.path or origin.query or origin.fragment or origin.username:
        raise ValueError('Photos sync requires loopback to a private node; remote URLs are forbidden')
    if not HASH.fullmatch(config.get(pin, '')):
        raise ValueError('The private node identity must be pinned')
    return config[node].rstrip('/'), config[pin]


def flag(value):
    if type(value) is bool or (type(value) is int and value in (0, 1)):
        return int(value)
    raise ValueError('Invalid shared photo state')


def text(value, limit):
    if not isinstance(value, str) or len(value.encode()) > limit:
        raise ValueError('Invalid shared text')
    return value


class Client:
    def __init__(self, library, config, identity_path=None, account=None):
        self.library = library
        self.config = config
        self.node, self.node_id = pinned(config, 'node', 'node_id')
        self.legacy_node, self.legacy_node_id = (pinned(config, 'legacy_node', 'legacy_node_id')
                                                 if config.get('legacy_node') else (self.node, self.node_id))
        # Photos never owns an account: the owner's one Daochi account holds
        # the shared photos. The content key opens copies made before that.
        self.owner = account or owner_account.load(config.get('account_database'))
        self.account = self.owner.account
        self.identity_path = Path(identity_path or library.root / 'private' / 'identity.json')
        if not self.identity_path.exists():
            atomic(self.identity_path, packed(dict(content_key=secrets.token_hex(32))).encode())
        self.identity = json.loads(self.identity_path.read_bytes())
        self.key = bytes.fromhex(self.identity['content_key'])
        if len(self.key) != 32:
            raise ValueError('Invalid Photos content key; existing keys were preserved')
        # The account Photos used before entity data still holds old copies.
        self.legacy = None
        if 'public' in self.identity or 'private' in self.identity:
            try:
                legacy = owner_account.Account(bytes.fromhex(self.identity['public']), bytes.fromhex(self.identity['private']))
            except (KeyError, TypeError, ValueError):
                raise ValueError('Invalid Photos account identity; existing keys were preserved') from None
            if legacy.account != self.owner.account:
                self.legacy = legacy
        self.device_path = self.identity_path.with_name('device.json')
        if not self.device_path.exists():
            atomic(self.device_path, packed(dict(key=device_seed().hex(), client='photos-' + secrets.token_hex(12))).encode())
        device = json.loads(self.device_path.read_bytes())
        self.device_key = bytes.fromhex(device['key'])
        self.device_public = device_public(self.device_key)
        self.device_id = hashlib.sha256(self.device_public).hexdigest()
        self.client_id = device['client']
        self.tokens = {}
        self.auth_lock = threading.Lock()
        self.checked = False
        self.record_keys = {}

    def raw(self, path, method='GET', body=None, headers=None, binary=False, limit=128 * 1024 * 1024, node=None):
        data = sync_http.request(node or self.node,path,method,body,headers,limit)
        if binary:
            return data
        return json.loads(data) if data else {}

    def check_node(self, node=None):
        node = node or self.node
        sync_http.check_node(node, self.node_id if node == self.node else self.legacy_node_id)
        self.checked = True

    def origin(self, account):
        # The earlier account's copies stay on the node that already has them.
        return self.legacy_node if account is self.legacy else self.node

    def login(self, account=None):
        account = account or self.owner
        node = self.origin(account)
        self.check_node(node)
        challenge = self.raw('/api/v1/sync/challenge?user_id=' + account.account, node=node)
        body, signature = sync_account.login(account.public, account.private, self.client_id,
                                             packed(challenge).encode())
        result = self.raw('/api/v1/sync/login', 'POST', body,
                          {'Content-Type': 'application/json', 'X-Daochi-User': account.account, 'X-Daochi-Signature': signature},
                          node=node)
        token = result['auth_token']
        body = sync_account.registration(account.public, account.private, self.device_key,
                                         self.client_id, int(time.time()))
        self.raw('/api/v1/account/devices', 'POST', body,
                 {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}, node=node)
        self.tokens[account.account] = token

    def header(self, account, method, path, body):
        return sync_transaction.header(account.public, account.private, self.device_key,
                                       method, path, body or b'', int(time.time()) + 120)

    def request(self, path, method='GET', body=None, binary=False, account=None):
        account = account or self.owner
        for attempt in range(2):
            if not self.tokens.get(account.account):
                with self.auth_lock:
                    if not self.tokens.get(account.account):
                        self.login(account)
            token = self.tokens[account.account]
            headers = {'Authorization': 'Bearer ' + token,
                       'X-Daochi-User': account.account, 'X-Daochi-Tx': self.header(account, method, path, body),
                       'Content-Type': 'application/octet-stream' if binary else 'application/json'}
            try:
                return self.raw(path, method, body, headers, binary, node=self.origin(account))
            except urllib.error.HTTPError as error:
                if error.code != 401 or attempt:
                    raise
                with self.auth_lock:
                    if self.tokens.get(account.account) == token:
                        self.tokens.pop(account.account, None)
                        self.login(account)
        raise ValueError('Local node authentication failed')

    # The account's shared content.

    def record_key(self, kind):
        if kind not in self.record_keys:
            self.record_keys[kind] = entity_crypto.record_key(self.owner.private, kind)
        return self.record_keys[kind]

    def put(self, app, data, account=None):
        digest = hashlib.sha256(data).hexdigest()
        path = f'/api/v1/blobs/{app}/{digest}'
        try:
            self.request(path, 'HEAD', binary=True, account=account)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            self.request(path, 'PUT', data, binary=True, account=account)
        return digest

    def put_content(self, digest, blocks):
        # The keys belong to the content, so the same photo always seals to
        # the same chunks and the node keeps one copy for every app.
        blobs, whole, size = [], hashlib.sha256(), 0
        for plain in blocks:
            whole.update(plain)
            size += len(plain)
            sealed = entity_crypto.seal_chunk(self.owner.private, 'photos', digest, len(blobs), plain)
            blobs.append(dict(address=self.put('entity', sealed), size=len(sealed)))
        if whole.hexdigest() != digest:
            raise ValueError('Local original changed; refusing to sync corrupted bytes')
        return blobs, size

    def get_content(self, digest, blobs, output=None):
        """Download, open and check content; without an output only check it."""
        whole, size = hashlib.sha256(), 0
        for index, blob in enumerate(blob_list(blobs)):
            sealed = self.request(f"/api/v1/blobs/entity/{blob['address']}", binary=True)
            if hashlib.sha256(sealed).hexdigest() != blob['address'] or len(sealed) != blob['size']:
                raise ValueError('Encrypted original failed its integrity check')
            plain = entity_crypto.open_chunk(self.owner.private, 'photos', digest, index, sealed)
            whole.update(plain)
            size += len(plain)
            if output is not None:
                output.write(plain)
        if whole.hexdigest() != digest:
            raise ValueError('Original download failed integrity verification')
        return size

    # Copies the Photos account made before entity data.

    def legacy_aad(self, photo_id, part):
        return packed(dict(account_id=self.legacy.account, app_id=APP, id=photo_id,
                           schema='photos.media.v1', part=part)).encode()

    def fetch_blob(self, digest, photo_id, part):
        if not HASH.fullmatch(digest) or self.legacy is None:
            raise ValueError('Invalid encrypted blob reference')
        data = self.request(f'/api/v1/blobs/{APP}/{digest}', binary=True, account=self.legacy)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Encrypted original failed its integrity check')
        return decrypt(data[24:], self.legacy_aad(photo_id, part), data[:24], self.key)

    def legacy_blocks(self, photo, manifest):
        for index, digest in enumerate(manifest['chunks']):
            yield self.fetch_blob(digest, photo['id'], f'chunk:{index}')

    def legacy_record(self, record):
        collection = record.get('collection') if isinstance(record, dict) else None
        if collection not in {LEGACY_MEDIA, LEGACY_ALBUMS} or not HASH.fullmatch(str(record.get('id', ''))):
            raise ValueError('Node returned an unsupported earlier photo record; the cursor was retained')
        if record.get('deleted_at'):
            return None
        schema = 'photos.album.v1' if collection == LEGACY_ALBUMS else 'photos.media.v1'
        aad = dict(account_id=self.legacy.account, app_id=APP, collection=collection, id=record['id'], schema=schema)
        try:
            value = self.open_envelope(record, 'main', aad, self.key)
            if value.get('id') != record['id']:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ValueError('Earlier encrypted record was moved or altered; the cursor was retained') from None
        return collection, value

    def legacy_photo(self, value):
        photo = {key: value.get(key) for key in ('id', 'name', 'extension', 'kind', 'size', 'taken', 'width', 'height',
                                                 'favorite', 'hidden', 'trashed', 'caption', 'tags', 'updated')}
        photo.update(archived=value.get('archived', 0), metadata=value.get('metadata', {}), albums=value.get('albums', []),
                     manifest=value.get('manifest', {}), edits=value.get('edits', {}))
        if (photo['extension'] not in IMAGES | VIDEOS or photo['kind'] not in ('photo', 'video')
                or not isinstance(photo['name'], str) or type(photo['size']) is not int or photo['size'] < 0
                or type(photo['width']) is not int or type(photo['height']) is not int
                or not isinstance(photo['caption'], str) or not isinstance(photo['tags'], str)
                or type(photo['trashed']) is not int or photo['trashed'] < 0
                or not isinstance(photo['metadata'], dict) or not isinstance(photo['manifest'], dict)
                or not isinstance(photo['edits'], dict) or not isinstance(photo['albums'], list)
                or any(not isinstance(name, str) for name in photo['albums'])):
            raise ValueError('Invalid earlier photo record; the cursor was retained')
        number(photo['taken'], -62135596800, 253402300800); number(photo['updated'])
        for key in ('favorite', 'hidden', 'archived'):
            photo[key] = flag(photo[key])
        # Its original moves from the earlier copy; nothing else carries over.
        photo['manifest'] = {key: photo['manifest'][key] for key in ('chunks', 'chunk_bytes', 'preview', 'preview_id')
                             if key in photo['manifest']}
        if not isinstance(photo['manifest'].get('chunks'), list):
            raise ValueError('Invalid earlier photo record; the cursor was retained')
        photo['edits'] = {key: edit for key, edit in photo['edits'].items() if key != 'auto_albums'}
        return photo

    def legacy_exchange(self):
        """Add photos and albums new under the earlier account, such as phone backups.

        Photos the library already has are never changed from there; new ones
        move to the shared entity data like every other photo.
        """
        db = self.library.db
        while True:
            since = self.library.setting('legacy_server_version', 0)
            cursor = self.library.setting('legacy_record_cursor', None)
            request = dict(protocol_version=6, app_id=APP, user_id_hash=self.legacy.account, client_id=self.client_id,
                           client_clock=since, since_server_version=since,
                           encrypted_records_only=True, record_limit=RECORD_PAGE, encrypted_records=[])
            if cursor:
                request['record_cursor'] = cursor
            result = self.request('/api/v1/sync', 'POST', packed(request).encode(), account=self.legacy)
            changes = result.get('changes') if isinstance(result, dict) else None
            incoming = changes.get('encrypted_records') if isinstance(changes, dict) else None
            complete = result.get('changes_complete') if isinstance(result, dict) else None
            version = result.get('server_version') if isinstance(result, dict) else None
            following = result.get('record_cursor') if isinstance(result, dict) else None
            if isinstance(changes, dict) and incoming is None:
                incoming = []
            if (not isinstance(incoming, list) or len(incoming) > RECORD_PAGE or result.get('status') != 'ok'
                    or result.get('full_snapshot_required') or type(version) is not int or version < since
                    or type(complete) is not bool or (not complete and (not isinstance(following, dict) or not incoming))):
                raise ValueError('Node returned an invalid earlier metadata page; the cursor was retained')
            values = [value for value in map(self.legacy_record, incoming) if value]
            photos = [self.legacy_photo(value) for collection, value in values if collection == LEGACY_MEDIA]
            albums = [value for collection, value in values if collection == LEGACY_ALBUMS]
            with db:
                db.execute('BEGIN IMMEDIATE')
                if (self.library.setting('legacy_server_version', 0) != since
                        or self.library.setting('legacy_record_cursor', None) != cursor):
                    raise ValueError('Earlier metadata changed during sync; the cursor was retained')
                for album in albums:
                    name = self.library.album_name(album.get('name'))
                    if not db.execute('SELECT 1 FROM albums WHERE name=?', (name,)).fetchone():
                        row = self.library._ensure_album(name, dirty=1)
                        db.execute('UPDATE albums SET description=?,pinned=?,archived=?,deleted=? WHERE id=?',
                                   (text(album.get('description', ''), 8192), flag(album.get('pinned', 0)),
                                    flag(album.get('archived', 0)), flag(album.get('deleted', 0)), row['id']))
                for photo in photos:
                    if db.execute('SELECT 1 FROM photos WHERE id=?', (photo['id'],)).fetchone():
                        continue
                    for name in photo['albums']:
                        self.library._ensure_album(name, dirty=1)
                    columns = list(photo)
                    db.execute('INSERT INTO photos(' + ','.join(columns) + ',dirty) VALUES(' + ','.join('?' for _ in columns) + ',0)',
                               [packed(photo[key]) if key in ('metadata', 'albums', 'manifest', 'edits') else photo[key] for key in columns])
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('legacy_server_version', packed(since if not complete else version)))
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('legacy_record_cursor', packed(None if complete else following)))
            if complete:
                return

    def verify(self, photo, entity):
        """Download a photo's shared copy again and check it against its hash."""
        if self.get_content(photo['id'], entity['blobs']) != photo['size']:
            raise ValueError('Shared original failed verification; old copies were kept')
        preview = entity.get('preview')
        if preview and self.get_content(preview['hash'], preview['blobs']) != preview['size']:
            raise ValueError('Shared preview failed verification; old copies were kept')

    def retire(self, maximum):
        """Delete old original chunks of photos whose shared copy checks out."""
        if self.legacy is None or self.config.get('retire_legacy_chunks') is not True:
            return 0
        db = self.library.db
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM photos WHERE json_extract(manifest,'$.entity.confirmed')=1 AND json_type(manifest,'$.chunks')='array' LIMIT ?",
            (maximum,))]
        for photo in rows:
            manifest = json.loads(photo['manifest'])
            self.verify(photo, manifest['entity'])
            for digest in manifest['chunks']:
                if HASH.fullmatch(digest):
                    self.request(f'/api/v1/blobs/{APP}/{digest}', 'DELETE', binary=True, account=self.legacy)
            current = db.execute('SELECT manifest FROM photos WHERE id=?', (photo['id'],)).fetchone()
            # The old record and preview stay readable for older releases.
            manifest = {key: value for key, value in json.loads(current[0]).items() if key not in ('chunks', 'chunk_bytes')}
            db.execute('UPDATE photos SET manifest=? WHERE id=?', (packed(manifest), photo['id']))
            db.commit()
        return len(rows)

    # Photos and albums as records.

    def prepare_entity(self, photo):
        manifest = json.loads(photo['manifest'])
        entity = manifest.get('entity')
        if isinstance(entity, dict) and isinstance(entity.get('blobs'), list):
            return entity
        source = self.library.original(photo)
        if source.exists():
            with source.open('rb') as incoming:
                blobs, size = self.put_content(photo['id'], iter(lambda: incoming.read(CHUNK), b''))
        elif isinstance(manifest.get('chunks'), list) and self.legacy is not None:
            # A library that relied on the node keeps its original there.
            blobs, size = self.put_content(photo['id'], self.legacy_blocks(photo, manifest))
        else:
            raise ValueError('Original is missing; sync will retry without marking it complete')
        if size != photo['size']:
            raise ValueError('Local original changed; refusing to sync corrupted bytes')
        entity = dict(blobs=blobs)
        thumbnail = self.library.thumbnail(photo['id'])
        preview = b''
        if thumbnail.exists():
            preview = thumbnail.read_bytes()
        elif manifest.get('preview') and self.legacy is not None:
            preview = self.fetch_blob(manifest['preview'], manifest['preview_id'], 'preview')
        if preview:
            digest = hashlib.sha256(preview).hexdigest()
            chunks, size = self.put_content(digest, [preview[at:at + CHUNK] for at in range(0, len(preview), CHUNK)])
            entity['preview'] = dict(hash=digest, size=size, type='image/jpeg', blobs=chunks)
        return entity

    def save_entity(self, photo, entity):
        manifest = json.loads(photo['manifest'])
        manifest['entity'] = entity
        self.library.db.execute('UPDATE photos SET manifest=? WHERE id=?', (packed(manifest), photo['id']))
        self.library.db.commit()

    def photo_value(self, photo, entity):
        edits = json.loads(photo['edits'])
        # Fields another app added stay in the shared record.
        value = dict(entity.get('record') or {})
        value.update(hash=photo['id'], size=photo['size'], type=media_type(photo), taken_at=exact(photo['taken']),
                     width=photo['width'], height=photo['height'], blobs=entity['blobs'],
                     name=photo['name'], trashed=bool(photo['trashed']), favorite=bool(photo['favorite']),
                     hidden=bool(photo['hidden']), archived=bool(photo['archived']),
                     caption=photo['caption'], tags=tag_list(photo['tags']),
                     edits={key: edit for key, edit in edits.items() if key not in LOCAL_EDITS},
                     albums=self.photo_albums(photo, value.get('albums')))
        value.setdefault('added_at', math.floor(photo['updated']))
        if entity.get('preview'):
            value['preview'] = entity['preview']
        else:
            value.pop('preview', None)
        return value

    def annotation_value(self, photo):
        from .semantic import encrypted_index
        from .people import export_index
        edits = {key: value for key, value in json.loads(photo['edits']).items() if key in LOCAL_EDITS}
        if 'auto_albums' in edits:
            # Each device has its own album IDs; the shared album's ID names it.
            shared = {}
            for album_id, people_ids in edits['auto_albums'].items():
                album = self.library.db.execute('SELECT id,entity FROM albums WHERE id=?', (album_id,)).fetchone()
                if album:
                    shared[self.album_entity(album)] = people_ids
            edits['auto_albums'] = shared
        return dict(id=photo['id'], metadata=json.loads(photo['metadata']), edits=edits,
                    semantic=encrypted_index(self.library, photo['id']), people=export_index(self.library, photo['id']))

    def photo_albums(self, photo, shared):
        # A photo names its shared albums, so an album of any size stays small.
        names = json.loads(photo['albums'])
        rows = self.library.db.execute('SELECT id,entity FROM albums WHERE name IN (SELECT value FROM json_each(?))',
                                       (packed(names),)).fetchall()
        albums = {self.album_entity(row) for row in rows}
        # Albums this device does not have stay named.
        if isinstance(shared, list):
            known = {row[0] for row in self.library.db.execute("SELECT entity FROM albums WHERE entity<>''")}
            albums |= {item for item in shared if isinstance(item, str) and ALBUM_ID.fullmatch(item) and item not in known}
        return sorted(albums)

    def album_entity(self, album):
        if album['entity']:
            return album['entity']
        entity = secrets.token_hex(16)
        self.library.db.execute("UPDATE albums SET entity=? WHERE id=? AND entity=''", (entity, album['id']))
        self.library.db.commit()
        return self.library.db.execute('SELECT entity FROM albums WHERE id=?', (album['id'],)).fetchone()[0]

    def album_value(self, album):
        value = dict(json.loads(album['record'] or '{}'))
        # Its photos name it; a list kept here could never hold a large album.
        value.pop('photos', None)
        value.update(name=album['name'], description=album['description'], cover=album['cover'],
                     pinned=bool(album['pinned']), archived=bool(album['archived']), deleted=bool(album['deleted']),
                     created_at=math.floor(album['created']), updated_at=math.floor(album['updated']))
        for key, current in (('filter', album['filter']), ('auto_people', json.loads(album['auto_people']))):
            if current:
                value[key] = current
            else:
                value.pop(key, None)
        return value

    def seal_entity(self, value, kind, record_id, schema, updated):
        collection = ENTITY + kind
        aad = dict(account_id=self.owner.account, app_id='entity', collection=collection, id=record_id, schema=schema)
        sealed = seal_record(packed(value).encode(), packed(aad).encode(), self.record_key(kind))
        return self.envelope(collection, record_id, 'entity-v1', aad, sealed, updated)

    def seal_annotation(self, value, updated):
        aad = dict(account_id=self.owner.account, app_id=APP, collection=ANNOTATIONS, id=value['id'],
                   schema='photos.annotations.v1')
        sealed = seal_record(packed(value).encode(), packed(aad).encode(), self.record_key('photos'))
        return self.envelope(ANNOTATIONS, value['id'], 'entity-v1', aad, sealed, updated)

    def envelope(self, collection, record_id, key_id, aad, sealed, updated):
        nonce, cipher = sealed[:24], sealed[24:]
        envelope = dict(profile='daochi-record-v1', alg='xchacha20poly1305', key_id=key_id,
                        nonce=encode(nonce), aad=aad, ciphertext=encode(cipher))
        return dict(collection=collection, id=record_id, key_id=key_id, nonce=encode(nonce),
                    ciphertext=encode(packed(envelope).encode()), schema_version=1, updated_at=stamp(updated))

    def records(self, photo, entity):
        return [self.seal_entity(self.photo_value(photo, entity), 'photos', photo['id'], 'entity.photo.v1', photo['updated']),
                self.seal_annotation(self.annotation_value(photo), photo['updated'])]

    def record(self, photo, entity=None):
        """A photo's shared record and Photos' annotation of it, sealed."""
        if entity is None:
            entity = self.prepare_entity(photo)
        return self.records(photo, entity)

    def album_record(self, album):
        row = self.library.db.execute('SELECT * FROM albums WHERE id=?', (album['id'],)).fetchone()
        album = {**dict(row or {}), **dict(album)}
        album.setdefault('record', '{}'); album.setdefault('entity', '')
        entity = self.album_entity(album) if row else album['entity'] or secrets.token_hex(16)
        return self.seal_entity(self.album_value(album), 'albums', entity, 'entity.album.v1', album['updated'])

    # Records from the node.

    def open_envelope(self, record, key_id, aad, key):
        envelope = json.loads(decode(record['ciphertext']))
        if (envelope.get('aad') != aad or envelope.get('nonce') != record.get('nonce') or envelope.get('key_id') != key_id
                or record.get('key_id') != key_id or envelope.get('alg') != 'xchacha20poly1305'):
            raise ValueError()
        value = json.loads(decrypt(decode(envelope['ciphertext']), packed(aad).encode(), decode(envelope['nonce']), key))
        if not isinstance(value, dict):
            raise ValueError()
        return value

    def updated(self, record):
        # The writer's time on the record; another app may keep stale fields.
        return number(datetime.fromisoformat(record['updated_at'].replace('Z', '+00:00')).timestamp())

    def incoming_photo(self, record):
        aad = dict(account_id=self.owner.account, app_id='entity', collection=PHOTOS, id=record['id'], schema='entity.photo.v1')
        value = self.open_envelope(record, 'entity-v1', aad, self.record_key('photos'))
        if value.get('hash') != record['id'] or type(value.get('size')) is not int or value['size'] < 0:
            raise ValueError()
        blobs = blob_list(value.get('blobs'))
        updated = self.updated(record)
        local = self.library.db.execute('SELECT * FROM photos WHERE id=?', (record['id'],)).fetchone()
        local = dict(local) if local else None
        manifest = json.loads(local['manifest']) if local else {}
        migrated = isinstance(manifest.get('entity'), dict) and manifest['entity'].get('confirmed') is True
        # Fields the record leaves out keep what this device knows.
        known = dict(name=local['name'], trashed=bool(local['trashed']), favorite=bool(local['favorite']),
                     hidden=bool(local['hidden']), archived=bool(local['archived']), caption=local['caption'],
                     tags=tag_list(local['tags']),
                     edits={k: v for k, v in json.loads(local['edits']).items() if k not in LOCAL_EDITS}) if local else {}
        person = {key: value[key] if key in value else known.get(key) for key in PERSON}
        kind = value.get('type') if isinstance(value.get('type'), str) else ''
        name = person['name'] if isinstance(person['name'], str) and person['name'] else record['id'] + EXTENSIONS.get(kind, '.jpg')
        extension = local['extension'] if local else Path(name).suffix.lower()
        if extension not in IMAGES | VIDEOS:
            extension = EXTENSIONS.get(kind, '.mp4' if kind.startswith('video/') else '.jpg')
        entity = dict(blobs=blobs, record=value, confirmed=migrated or local is None)
        if isinstance(value.get('preview'), dict):
            preview = value['preview']
            if not isinstance(preview.get('hash'), str) or not HASH.fullmatch(preview['hash']) or type(preview.get('size')) is not int:
                raise ValueError()
            entity['preview'] = dict(preview, blobs=blob_list(preview.get('blobs')))
        manifest['entity'] = entity
        if local and not migrated:
            # A record that already exists for an unmoved photo is kept; this
            # device adds what it knows when it moves the photo.
            updated = max(updated, local['updated'] + 0.000001)
        # The native merge keeps this device's metadata and own edits, and
        # finds the albums the record names among this device's albums.
        edits = {key: edit for key, edit in person['edits'].items() if key not in LOCAL_EDITS} if isinstance(person['edits'], dict) else {}
        albums = json.loads(local['albums']) if local else []
        named = value.get('albums')
        if named is not None and (not isinstance(named, list) or len(named) > 4096 or any(
                not isinstance(item, str) or not ALBUM_ID.fullmatch(item) for item in named)):
            raise ValueError()
        trashed = flag(person['trashed'] or False)
        if trashed:
            trashed = local['trashed'] if local and local['trashed'] else int(time.time())
        tags = person['tags'] if isinstance(person['tags'], list) else []
        return dict(id=record['id'], name=name, extension=extension,
                    kind='video' if extension in VIDEOS else 'photo', size=value['size'],
                    taken=number(value.get('taken_at', local['taken'] if local else 0), -62135596800, 253402300800),
                    width=int(value.get('width') or 0), height=int(value.get('height') or 0),
                    metadata=json.loads(local['metadata']) if local else {}, albums=sorted(set(albums)),
                    favorite=flag(person['favorite'] or False), hidden=flag(person['hidden'] or False),
                    archived=flag(person['archived'] or False), trashed=trashed,
                    caption=text(person['caption'] or '', 65536),
                    tags=', '.join(text(tag, 512) for tag in tags if tag),
                    updated=updated, manifest=manifest, edits=edits, album_ids=named or [],
                    # An unmoved photo, or a record from an app that does not
                    # know albums, keeps this device's albums too.
                    albums_mode='record' if named is not None and migrated else 'union')

    def incoming_album(self, record):
        aad = dict(account_id=self.owner.account, app_id='entity', collection=ALBUMS, id=record['id'], schema='entity.album.v1')
        value = self.open_envelope(record, 'entity-v1', aad, self.record_key('albums'))
        # Albums written before photos named them may still list their photos.
        photos = value.get('photos', [])
        if not isinstance(photos, list) or any(not isinstance(item, str) or not HASH.fullmatch(item) for item in photos):
            raise ValueError()
        name = self.library.album_name(value.get('name'))
        if name != value.get('name'):
            raise ValueError()
        db = self.library.db
        local = db.execute('SELECT id FROM albums WHERE entity=?', (record['id'],)).fetchone()
        if local is None:
            same = db.execute('SELECT id,entity FROM albums WHERE name=?', (name,)).fetchone()
            if same is not None and not same['entity']:
                local = same  # An album of the same name made here before it was shared.
            elif same is not None:
                # Two shared albums of one name; this device shows the second apart.
                count = 2
                while db.execute('SELECT 1 FROM albums WHERE name=?', (f'{name} ({count})',)).fetchone():
                    count += 1
                name = f'{name} ({count})'
        album_id = local['id'] if local else hashlib.sha256(('photos.entity.album:' + record['id']).encode()).hexdigest()
        album = dict(id=album_id, entity=record['id'], name=name, description=text(value.get('description', ''), 8192),
                     cover=value.get('cover', ''), pinned=flag(value.get('pinned', False)),
                     archived=flag(value.get('archived', False)), deleted=flag(value.get('deleted', False)),
                     created=number(value.get('created_at', 0)), updated=self.updated(record),
                     filter=value.get('filter', ''), photos=photos, record=value)
        if 'auto_people' in value:
            album['auto_people'] = value['auto_people']
        if album['filter']:
            from .query import validate
            validate(self.library, album['filter'])
        return album

    def incoming_annotation(self, record):
        aad = dict(account_id=self.owner.account, app_id=APP, collection=ANNOTATIONS, id=record['id'],
                   schema='photos.annotations.v1')
        value = self.open_envelope(record, 'entity-v1', aad, self.record_key('photos'))
        if value.get('id') != record['id']:
            raise ValueError()
        annotation = dict(id=record['id'], updated=self.updated(record))
        for key in ('metadata', 'edits', 'semantic', 'people'):
            if key in value:
                annotation[key] = value[key]
        if isinstance(annotation.get('edits'), dict) and 'auto_albums' in annotation['edits']:
            # Named by shared album IDs; the native merge finds this device's albums.
            from .auto_albums import identities
            shared = annotation['edits']['auto_albums']
            if not isinstance(shared, dict) or len(shared) > 128 or any(
                    not isinstance(key, str) or not ALBUM_ID.fullmatch(key) for key in shared):
                raise ValueError()
            annotation['edits']['auto_albums'] = {key: identities(value) for key, value in shared.items()}
        return annotation

    def open_record(self, record):
        collection = record.get('collection') if isinstance(record, dict) else None
        if not isinstance(collection, str) or not isinstance(record.get('id'), str):
            raise ValueError('Node returned an unsupported photo record; the cursor was retained')
        if collection.startswith(ENTITY) and collection not in {PHOTOS, ALBUMS}:
            return None  # Another kind of the account's data, such as levels.
        identifier = ALBUM_ID if collection == ALBUMS else HASH
        if collection not in {PHOTOS, ALBUMS, ANNOTATIONS} or not identifier.fullmatch(record['id']):
            raise ValueError('Node returned an unsupported photo record; the cursor was retained')
        if record.get('deleted_at'):
            # Keep originals in Trash; physical deletion needs a separate policy.
            return None
        try:
            if collection == PHOTOS:
                return self.incoming_photo(record)
            if collection == ALBUMS:
                return self.incoming_album(record)
            return self.incoming_annotation(record)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError('Encrypted record metadata was moved or altered; the cursor was retained') from None

    def accept(self, records):
        """Merge records that arrived outside a sync page, all or none."""
        records = records if isinstance(records, list) else [records]
        if not 0 < len(records) <= RECORD_PAGE or self.library.setting('record_cursor', None):
            raise ValueError('Finish the current sync page first; nothing was merged')
        since = self.library.setting('server_version', 0)
        result = dict(status='ok', encrypted_records_only=True, server_version=since, changes_complete=True,
                      changes=dict(encrypted_records=records))
        sync_pages.apply(self.library, result, since, None, [self.open_record(record) for record in records], len(records))

    def accept_album(self, record):
        self.accept([record])

    def exchange(self, records=None):
        uploaded = None
        while True:
            since = self.library.setting('server_version', 0)
            cursor = self.library.setting('record_cursor', None)
            request = dict(protocol_version=6, app_id=APP, user_id_hash=self.owner.account, client_id=self.client_id,
                           client_clock=since, since_server_version=since,
                           encrypted_records_only=True, record_limit=RECORD_PAGE, encrypted_records=records or [])
            if cursor:
                request['record_cursor'] = cursor
            result = self.request('/api/v1/sync', 'POST', packed(request).encode())
            changes = result.get('changes') if isinstance(result, dict) else None
            incoming = changes.get('encrypted_records') if isinstance(changes, dict) else None
            if isinstance(changes, dict) and incoming is None:
                incoming = []  # Daochi represents an empty record list as null.
            if not isinstance(incoming, list) or len(incoming) > RECORD_PAGE:
                raise ValueError('Node returned an invalid metadata page')
            decoded = [self.open_record(record) for record in incoming]
            # Native Ziran compares the request checkpoint with a fresh writer
            # snapshot, validates the page and merges records/indexes/cursor in
            # one transaction. Failed decryption never reaches that writer.
            sync_pages.apply(self.library, result, since, cursor, decoded, RECORD_PAGE)
            uploaded = uploaded or result
            records = None
            if result.get('changes_complete') is True:
                return uploaded

    def switch_account(self):
        """Sync the owner's account from its start; keep the old checkpoint."""
        if self.library.setting('sync_account') == self.owner.account:
            return
        db = self.library.db
        with db:
            db.execute('BEGIN IMMEDIATE')
            for key in ('server_version', 'record_cursor'):
                row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
                if row and not db.execute('SELECT 1 FROM settings WHERE key=?', ('legacy_' + key,)).fetchone():
                    db.execute('INSERT INTO settings VALUES(?,?)', ('legacy_' + key, row[0]))
                db.execute('DELETE FROM settings WHERE key=?', (key,))
            # Every album becomes a shared album record of the account.
            db.execute('UPDATE albums SET dirty=1')
            db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('sync_account', packed(self.owner.account)))

    def sync(self, maximum=50):
        self.check_node()
        self.switch_account()
        if self.legacy is not None:
            try:
                self.legacy_exchange()
            except (OSError, urllib.error.URLError):
                pass  # The earlier copies wait for the ThinkPad; shared sync goes on.
        self.exchange()
        db = self.library.db
        rows = [dict(row) for row in db.execute('SELECT * FROM photos WHERE dirty=1 ORDER BY updated LIMIT ?', (maximum,))]
        # Photos synced before entity data move a few at a time, so the node
        # never needs room for a second copy of the whole library at once.
        if len(rows) < maximum:
            rows += [dict(row) for row in db.execute(
                "SELECT * FROM photos WHERE dirty=0 AND coalesce(json_extract(manifest,'$.entity.confirmed'),0)=0 ORDER BY updated LIMIT ?",
                (maximum - len(rows),))]
        album_rows = [dict(row) for row in db.execute('SELECT * FROM albums WHERE dirty=1 ORDER BY updated LIMIT ?', (maximum,))]
        records = []
        for album in album_rows:
            album['entity'] = self.album_entity(album)
            records.append(self.album_record(album))
        entities = {}
        if rows:
            self.library.set_setting('status', 'Syncing originals to your private node')
            # Workers handle immutable files and encrypted HTTP only. All SQLite
            # access and metadata edits remain on the owning thread.
            with ThreadPoolExecutor(max_workers=4) as uploads:
                futures = {uploads.submit(self.prepare_entity, row): row for row in rows}
                for future in as_completed(futures):
                    photo = futures[future]
                    try:
                        entity = future.result()
                    except (OSError, urllib.error.URLError):
                        if photo['dirty']:
                            raise
                        continue  # An unmoved photo whose earlier copy is unreachable waits.
                    self.save_entity(photo, entity)
                    entities[photo['id']] = entity
                    records.extend(self.records(photo, entity))
        if records:
            # Face-rich photos carry larger encrypted records. Split by byte
            # budget as well as record count to fit the pinned node's request
            # limit; failed batches retain every dirty marker for safe retry.
            batches, batch, size = [], [], 0
            for record in records:
                length = len(packed(record).encode()) + 1
                if length>750000: raise ValueError('Photo metadata exceeds the encrypted sync page; it remains queued')
                if batch and (size+length>750000 or len(batch) == UPLOAD_RECORDS):
                    batches.append(batch); batch, size = [], 0
                batch.append(record); size += length
            if batch: batches.append(batch)
            for batch in batches:
                result = self.exchange(batch)
                if result.get('full_snapshot_required') or result.get('status') not in {'ok', 'success', 'synced'}:
                    raise ValueError('Node did not confirm the upload; it remains queued')
            for row in rows:
                if row['id'] not in entities:
                    continue
                entity = dict(entities[row['id']], record=self.photo_value(row, entities[row['id']]), confirmed=True)
                current = db.execute('SELECT manifest FROM photos WHERE id=?', (row['id'],)).fetchone()
                manifest = json.loads(current[0]) if current else {}
                manifest['entity'] = entity
                manifest['annotated'] = row['updated']
                db.execute('UPDATE photos SET manifest=?,dirty=CASE WHEN updated=? THEN 0 ELSE dirty END WHERE id=?',
                           (packed(manifest), row['updated'], row['id']))
            for album in album_rows:
                db.execute('UPDATE albums SET dirty=0,record=? WHERE id=? AND updated=?',
                           (packed(self.album_value(album)), album['id'], album['updated']))
            db.commit()
        self.retire(maximum)
        pending = db.execute("SELECT COUNT(*) FROM photos WHERE dirty=1 OR coalesce(json_extract(manifest,'$.entity.confirmed'),0)=0").fetchone()[0]
        pending += db.execute('SELECT COUNT(*) FROM albums WHERE dirty=1').fetchone()[0]
        self.library.set_setting('status', f'Private node sync · {pending:,} queued' if pending else 'All photos synced · private nodes only')
        return pending

    def materialize(self, photo_id, preview_only=False):
        photo = self.library.photo(photo_id)
        manifest = json.loads(photo['manifest'])
        entity = manifest.get('entity') if isinstance(manifest.get('entity'), dict) else {}
        thumbnail = self.library.thumbnail(photo_id)
        if not thumbnail.exists():
            if isinstance(entity.get('preview'), dict):
                self.download(entity['preview']['hash'], entity['preview']['blobs'], thumbnail, entity['preview']['size'])
            elif manifest.get('preview') and self.legacy is not None:
                atomic(thumbnail, self.fetch_blob(manifest['preview'], manifest['preview_id'], 'preview'))
        if preview_only:
            return str(thumbnail) if thumbnail.exists() else ''
        destination = self.library.original(photo)
        if destination.exists():
            return str(destination)
        if isinstance(entity.get('blobs'), list):
            self.download(photo_id, entity['blobs'], destination, photo['size'])
        else:
            chunks = manifest.get('chunks')
            if not isinstance(chunks, list) or (not chunks and photo['size'] != 0) or (chunks and self.legacy is None):
                raise ValueError('This original has not finished syncing yet')
            self.download(photo_id, None, destination, photo['size'], chunks)
        self.library.preview(photo, destination)
        return str(destination)

    def download(self, digest, blobs, destination, expected, legacy=None):
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix='.download-', dir=destination.parent)
        try:
            with os.fdopen(fd, 'wb') as output:
                if legacy is None:
                    size = self.get_content(digest, blobs, output)
                else:
                    value, size = hashlib.sha256(), 0
                    for index, chunk in enumerate(legacy):
                        block = self.fetch_blob(chunk, digest, f'chunk:{index}')
                        value.update(block)
                        size += len(block)
                        output.write(block)
                    if value.hexdigest() != digest:
                        raise ValueError('Original download failed integrity verification')
                output.flush()
                os.fsync(output.fileno())
            if size != expected:
                raise ValueError('Original download failed integrity verification')
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
