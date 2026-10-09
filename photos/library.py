"""A merged, content-addressed library. Imported folders are always read-only."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat as file_stat
import subprocess
import tempfile
import time
from datetime import datetime, timezone

from PIL import Image, ImageOps, UnidentifiedImageError

IMAGES = {'.jpg', '.jpeg', '.jpe', '.png', '.webp', '.gif', '.heic', '.heif',
          '.tif', '.tiff', '.bmp', '.avif', '.hif', '.dng', '.cr2', '.cr3', '.nef', '.nrw', '.arw',
          '.sr2', '.srf', '.raf', '.rw2', '.rwl', '.orf', '.pef', '.ptx', '.srw', '.3fr', '.fff',
          '.iiq', '.kdc', '.dcr', '.erf', '.mrw', '.mos', '.mef'}
VIDEOS = {'.mp4', '.mov', '.m4v', '.3gp', '.avi', '.webm', '.mpg', '.mpeg', '.mod', '.flv', '.mkv', '.wmv'}


class LibraryError(ValueError):
    """A local validation message safe to show in the native editor."""


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.next-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def digest_file(path):
    value = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


class Library:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.root / 'library.sqlite', timeout=60)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS photos (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, extension TEXT NOT NULL,
                kind TEXT NOT NULL, size INTEGER NOT NULL, taken REAL NOT NULL,
                width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0,
                metadata TEXT NOT NULL, albums TEXT NOT NULL,
                favorite INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                trashed INTEGER NOT NULL DEFAULT 0, caption TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '', updated REAL NOT NULL,
                dirty INTEGER NOT NULL DEFAULT 1, manifest TEXT NOT NULL DEFAULT '{}');
            CREATE INDEX IF NOT EXISTS photo_dates ON photos(taken DESC, id);
            CREATE TABLE IF NOT EXISTS sources (
                path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER,
                meta_stamp TEXT, photo_id TEXT REFERENCES photos(id));
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS motion_scans (
                photo_id TEXT PRIMARY KEY, reader_version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS semantic_embeddings (
                photo_id TEXT PRIMARY KEY, model TEXT NOT NULL,
                embedding BLOB, error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS people (
                id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
                ignored INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL,
                birth TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS face_scans (
                photo_id TEXT PRIMARY KEY, model TEXT NOT NULL, error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS faces (
                id TEXT PRIMARY KEY, photo_id TEXT NOT NULL, person_id TEXT NOT NULL,
                rect TEXT NOT NULL, embedding BLOB NOT NULL,
                excluded TEXT NOT NULL DEFAULT '[]');
            CREATE INDEX IF NOT EXISTS faces_person ON faces(person_id,photo_id);
            CREATE INDEX IF NOT EXISTS faces_photo ON faces(photo_id);
            CREATE TABLE IF NOT EXISTS person_tags (
                photo_id TEXT NOT NULL, person_id TEXT NOT NULL,
                PRIMARY KEY(photo_id,person_id));
            CREATE INDEX IF NOT EXISTS person_tags_person ON person_tags(person_id,photo_id);
            CREATE TABLE IF NOT EXISTS person_tag_edits (photo_id TEXT PRIMARY KEY);
            CREATE VIEW IF NOT EXISTS person_photos AS
                SELECT photo_id,person_id FROM faces UNION SELECT photo_id,person_id FROM person_tags;
            CREATE TABLE IF NOT EXISTS albums (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, description TEXT NOT NULL DEFAULT '',
                cover TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, updated REAL NOT NULL, dirty INTEGER NOT NULL DEFAULT 1);
        ''')
        for column, declaration in [('edits', "TEXT NOT NULL DEFAULT '{}'"),
                                    ('archived', 'INTEGER NOT NULL DEFAULT 0')]:
            if column in {row[1] for row in self.db.execute('PRAGMA table_info(photos)')}:
                continue
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                if column not in {row[1] for row in self.db.execute('PRAGMA table_info(photos)')}:
                    self.db.execute(f'ALTER TABLE photos ADD COLUMN {column} {declaration}')
        if 'filter' not in {row[1] for row in self.db.execute('PRAGMA table_info(albums)')}:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                if 'filter' not in {row[1] for row in self.db.execute('PRAGMA table_info(albums)')}:
                    self.db.execute("ALTER TABLE albums ADD COLUMN filter TEXT NOT NULL DEFAULT ''")
        self.db.commit()
        if 'auto_people' not in {row[1] for row in self.db.execute('PRAGMA table_info(albums)')}:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                if 'auto_people' not in {row[1] for row in self.db.execute('PRAGMA table_info(albums)')}:
                    self.db.execute("ALTER TABLE albums ADD COLUMN auto_people TEXT NOT NULL DEFAULT '[]'")
        if 'birth' not in {row[1] for row in self.db.execute('PRAGMA table_info(people)')}:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                if 'birth' not in {row[1] for row in self.db.execute('PRAGMA table_info(people)')}:
                    self.db.execute("ALTER TABLE people ADD COLUMN birth TEXT NOT NULL DEFAULT ''")
        self.import_album_names = {}
        self.import_reader = None
        if not self.setting('album_catalog_migrated', False):
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                if not self.setting('album_catalog_migrated', False):
                    for row in self.db.execute('SELECT DISTINCT value FROM photos,json_each(photos.albums)').fetchall():
                        self._ensure_album(row[0])
                    self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('album_catalog_migrated', 'true'))

    def setting(self, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, packed(value)))
        self.db.commit()

    def photo(self, photo_id):
        row = self.db.execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
        if row is None:
            raise LibraryError('Photo not found')
        return dict(row)

    def original(self, photo):
        return self.root / 'originals' / photo['id'][:2] / (photo['id'] + photo['extension'])

    def thumbnail(self, photo_id):
        return self.root / 'previews' / (photo_id + '.jpg')

    def preview(self, photo, source):
        target = self.thumbnail(photo['id'])
        if target.exists():
            return
        image = None
        try:
            if photo['kind'] == 'photo':
                from . import decoding
                if decoding.supported(photo['extension']):
                    # Generate only the small gallery asset during import.
                    # Full display pixels are decoded on explicit opening.
                    fd, name = tempfile.mkstemp(suffix='.png')
                    os.close(fd)
                    temporary = Path(name)
                    try:
                        dimensions = decoding.decode(source, photo['extension'], temporary, thumbnail=True)
                        with Image.open(temporary) as opened:
                            image = opened.convert('RGB')
                        self.db.execute('UPDATE photos SET width=?,height=?,updated=?,dirty=1 WHERE id=? AND (width<>? OR height<>?)',
                                        (dimensions['width'], dimensions['height'], time.time(), photo['id'], dimensions['width'], dimensions['height']))
                    finally:
                        temporary.unlink(missing_ok=True)
                else:
                    with Image.open(source) as opened:
                        image = ImageOps.exif_transpose(opened).convert('RGB')
                        image.thumbnail((640, 640))
            elif shutil.which('ffmpeg'):
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = target.with_suffix('.next.jpg')
                result = subprocess.run(['ffmpeg', '-loglevel', 'error', '-nostdin', '-y',
                                         '-i', str(source), '-frames:v', '1', '-vf',
                                         'scale=640:640:force_original_aspect_ratio=decrease',
                                         str(temporary)], capture_output=True, timeout=40)
                if result.returncode == 0:
                    os.replace(temporary, target)
                temporary.unlink(missing_ok=True)
                return
            if image is not None:
                from io import BytesIO
                output = BytesIO()
                image.save(output, 'JPEG', quality=85)
                atomic(target, output.getvalue())
        except (OSError, ValueError, UnidentifiedImageError, subprocess.TimeoutExpired):
            # Failed codecs never prevent retaining and backing up an original.
            return

    def sidecar(self, path):
        from . import importing
        if self.import_reader is None:
            with importing.Reader() as reader:
                return importing.sidecar(path, reader)
        return importing.sidecar(path, self.import_reader)

    def import_file(self, path, source_root):
        if self.import_reader is None:
            from . import importing
            with importing.Reader() as reader:
                self.import_reader = reader
                try:
                    return self._import_file(path, source_root)
                finally:
                    self.import_reader = None
        return self._import_file(path, source_root)

    def _import_file(self, path, source_root):
        path = Path(path)
        if path.is_symlink() or path.suffix.lower() not in IMAGES | VIDEOS:
            return 'skipped'
        stat = path.stat()
        if not file_stat.S_ISREG(stat.st_mode):
            return 'skipped'
        warning = False
        try:
            metadata, stamp = self.sidecar(path)
        except (OSError, ValueError):
            metadata, stamp = {}, 'invalid'
            warning = True
        cached = self.db.execute('SELECT * FROM sources WHERE path=?', (str(path),)).fetchone()
        if cached and cached['size'] == stat.st_size and cached['mtime'] == stat.st_mtime_ns and cached['meta_stamp'] == stamp:
            from . import decoding
            if decoding.supported(path.suffix.lower()) and not self.thumbnail(cached['photo_id']).is_file():
                photo = self.photo(cached['photo_id'])
                self.preview(photo, self.original(photo))
                self.db.commit()
            from . import motion
            try:
                motion.detect(self, cached['photo_id'], unchanged=True)
            except LibraryError:
                return 'warning'
            return 'warning' if warning else 'unchanged'
        photo_id = digest_file(path)
        existing = self.db.execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
        taken, width, height = stat.st_mtime, 0, 0
        try:
            from . import importing
            fields = importing.camera(path, taken, self.import_reader)
            taken = fields['taken']
            if path.suffix.lower() in IMAGES:
                width, height = fields['width'], fields['height']
                if fields['gps'] is not None and 'geoDataExif' not in metadata:
                    metadata['geoDataExif'] = fields['gps']
        except LibraryError:
            warning = True
        relative = path.relative_to(source_root)
        album = self.import_album_names.get(str(path.parent), str(relative.parent) if relative.parent != Path('.') else source_root.name)
        if not existing:
            photo = dict(id=photo_id, extension=path.suffix.lower(), kind='video' if path.suffix.lower() in VIDEOS else 'photo')
            destination = self.original(photo)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            from . import importing
            importing.copy(path, destination, photo_id, stat.st_size)
        from . import importing
        face_index = metadata.pop('photosPeople', None)
        automatic = metadata.pop('photosAutoAlbums', {})
        try:
            from .auto_albums import identities
            if not isinstance(automatic, dict) or len(automatic) > 128:
                raise LibraryError('Invalid automatic album memberships')
            automatic = {self.album_name(name): identities(selected) for name, selected in automatic.items()}
        except LibraryError:
            automatic = {}; warning = True
        exported_albums = metadata.get('albums', [])
        if not isinstance(exported_albums, list):
            exported_albums = []
        albums = sorted({album} | {name for name in exported_albums
                        if isinstance(name, str) and name.strip() and len(name.encode()) <= 512})
        saved = importing.merge(self, dict(id=photo_id, name=path.name, extension=path.suffix.lower(),
            kind='video' if path.suffix.lower() in VIDEOS else 'photo', size=stat.st_size, taken=taken,
            width=width, height=height, metadata=metadata, albums=albums,
            favorite=album.casefold() in {'favorites', 'favourites'} or metadata.get('favorite') is True,
            archived=metadata.get('archived') is True, caption=str(metadata.get('description', '')),
            tags=metadata.get('tags', '') if isinstance(metadata.get('tags', ''), str) else '',
            correct_date=bool(metadata.get('photoTakenTime')), path=str(path),
            mtime=stat.st_mtime_ns, stamp=stamp, auto_memberships=automatic, people_index=face_index), report=True)
        added = bool(saved['added']); warning = warning or bool(saved.get('warning', 0))
        if added:
            photo = self.photo(photo_id)
            self.preview(photo, self.original(photo))
            self.db.commit()
        from . import motion
        try:
            motion.detect(self, photo_id)
        except LibraryError:
            warning = True
        return 'warning' if warning else ('added' if added else 'merged')

    def import_folders(self, folders):
        if self.import_reader is None:
            from . import importing
            with importing.Reader() as reader:
                self.import_reader = reader
                try:
                    return self._import_folders(folders)
                finally:
                    self.import_reader = None
        return self._import_folders(folders)

    def _import_folders(self, folders):
        result = dict(added=0, merged=0, unchanged=0, skipped=0, warning=0, errors=0)
        failures = []
        for folder in folders:
            root = Path(folder).expanduser().resolve()
            if not root.is_dir() or root == self.root or self.root.is_relative_to(root) or root.is_relative_to(self.root):
                raise ValueError('Choose an existing source folder outside the Photos library')
            from . import importing
            for kind, path in importing.walk(root):
                if kind == 'error':
                    result['errors'] += 1
                    failures.append(dict(path=str(path), error='FolderDiscoveryError'))
                    continue
                if kind == 'directory':
                    try:
                        info = importing.album_metadata(path/'.photos-album.json', self.import_reader)
                        if info is None:
                            continue
                        name = self.album_name(info.get('name'))
                        description = info.get('description', '')
                        cover = info.get('cover', '')
                        smart_filter = info.get('filter', '')
                        from .auto_albums import identities
                        auto_people = identities(info.get('auto_people',[]))
                        if smart_filter and auto_people:
                            raise LibraryError('Smart albums cannot auto-add people')
                        if not isinstance(smart_filter, str):
                            raise LibraryError('Invalid smart album search')
                        if smart_filter:
                            from .query import validate
                            validate(self, smart_filter)
                        if not isinstance(description, str) or len(description.encode()) > 8192 or not isinstance(cover, str) or cover and (len(cover) != 64 or any(c not in '0123456789abcdef' for c in cover)):
                            raise LibraryError('Invalid album metadata')
                        if any(type(info.get(k, 0)) not in {int, bool} or info.get(k, 0) not in (0, 1) for k in ['pinned', 'archived']):
                            raise LibraryError('Invalid album state')
                        importing.merge(self, dict(operation='album', name=name, description=description,
                            cover=cover, filter=smart_filter, auto_people=auto_people,
                            pinned=bool(info.get('pinned', 0)), archived=bool(info.get('archived', 0))))
                        self.import_album_names[str(path)] = name
                    except (ValueError, OSError, AttributeError):
                        result['warning'] += 1
                if kind == 'directory':
                    continue
                try:
                    result[self.import_file(path, root)] += 1
                except (OSError, ValueError, sqlite3.Error) as error:
                    self.db.rollback()
                    result['errors'] += 1
                    failures.append(dict(path=str(path), error=type(error).__name__))
                if sum(result.values()) % 100 == 0:
                    self.set_setting('import', result)
        self.set_setting('import', result)
        self.set_setting('import_failures', failures)
        from . import live
        live.detect(self)
        return result

    def edit(self, photo_id, action, value=None, authorize=None):
        self.edit_many([photo_id], action, value, authorize=authorize)

    def edit_many(self, photo_ids, action, value=None, authorize=None):
        from . import native_edit
        if action not in native_edit.ACTIONS:
            raise LibraryError('Unknown photo action')
        return native_edit.change(self, photo_ids, action, value, authorize)

    @staticmethod
    def album_name(value):
        if not isinstance(value, str) or not value.strip() or len(value.strip().encode()) > 512 or any(ord(c) < 32 for c in value):
            raise LibraryError('Enter an album name of at most 512 bytes')
        return value.strip()

    def rename_album(self, old, new):
        from . import native_edit
        old, new = self.album_name(old), self.album_name(new)
        if old != new:
            native_edit.album_change(self, old, 'rename', new)

    def _edit(self, photo_id, action, value):
        # Automatic people-rule cleanup still shares its caller's transaction.
        # Ordinary user edits and album renames use the native writer above.
        if action != 'album_remove' or not self.db.in_transaction:
            raise LibraryError('Unknown internal album operation')
        photo = self.photo(photo_id)
        name = self.album_name(value)
        albums = set(json.loads(photo['albums'])); albums.discard(name)
        edits = json.loads(photo['edits'])
        removed = set(edits.get('removed_albums', [])); removed.add(name)
        definition = self.db.execute('SELECT id FROM albums WHERE name=?', (name,)).fetchone()
        if definition:
            edits.get('auto_albums', {}).pop(definition['id'], None)
        edits['removed_albums'] = sorted(removed)
        self.db.execute('UPDATE photos SET albums=?,edits=?,updated=max(updated+0.000001,?),dirty=1 WHERE id=?',
                        (packed(sorted(albums)), packed(edits), time.time(), photo_id))

    def _ensure_album(self, name, dirty=1):
        name = self.album_name(name)
        existing = self.db.execute('SELECT * FROM albums WHERE name=?', (name,)).fetchone()
        if existing:
            return existing
        album_id = hashlib.sha256(('photos.album.v1:' + name).encode()).hexdigest()
        # A renamed album retains its identity. Later imports into its old
        # folder need a distinct album rather than an orphaned membership.
        while self.db.execute('SELECT 1 FROM albums WHERE id=?', (album_id,)).fetchone():
            album_id = hashlib.sha256((album_id + ':next').encode()).hexdigest()
        now = time.time() if dirty else 0
        self.db.execute('INSERT OR IGNORE INTO albums(id,name,created,updated,dirty) VALUES(?,?,?,?,?)',
                        (album_id, name, now, now, dirty))
        # A tombstone prevents stale source scans from recreating a deleted album.
        return self.db.execute('SELECT * FROM albums WHERE name=?', (name,)).fetchone()

    def album_details(self, name):
        row = self.db.execute('SELECT * FROM albums WHERE name=? AND deleted=0', (name,)).fetchone()
        if not row:
            raise LibraryError('Album not found')
        return dict(row)

    def edit_album(self, name, action, value=None, search='', source=''):
        from . import native_edit
        return native_edit.album_change(self, name, action, value, search, source)

    def album_list(self, mode='active', sort='name', search=''):
        if mode not in {'active', 'archived', 'empty', 'all', 'places'} or sort not in {'name', 'newest', 'oldest'}:
            raise LibraryError('Choose a valid album filter and ordering')
        members = {}
        for row in self.db.execute('''SELECT p.id,p.taken,j.value FROM photos p,json_each(p.albums) j
                                      WHERE p.trashed=0 AND p.hidden=0
                                      AND coalesce(json_extract(p.metadata,'$.photosLive.photo'),'')=''
                                      AND (json_extract(p.metadata,'$.photosLive.video') IS NULL OR EXISTS(
                                        SELECT 1 FROM photos m WHERE m.id=json_extract(p.metadata,'$.photosLive.video')
                                        AND m.kind='video' AND m.hidden=0 AND m.trashed=0
                                        AND json_extract(m.metadata,'$.photosLive.photo')=p.id))
                                      ORDER BY p.taken DESC,p.id'''):
            members.setdefault(row['value'], []).append(row['id'])
        result = []
        for row in self.db.execute('SELECT * FROM albums WHERE deleted=0'):
            album = dict(row)
            from .places import is_place
            place = is_place(album['filter'])
            if mode == 'places' and not place:
                continue
            ids = members.get(album['name'], [])
            if search and search.casefold() not in album['name'].casefold():
                continue
            if mode == 'active' and album['archived'] or mode == 'archived' and not album['archived']:
                continue
            if album['filter']:
                from .query import page
                matched = page(self, view='photos', filter=album['filter'], sort='newest', limit=1)
                ids = matched['ids']
                count = matched['count']
            else:
                count = len(ids)
            if mode == 'empty' and (album['archived'] or count):
                continue
            cover = album['cover'] if album['cover'] in ids else (ids[0] if ids else '')
            preview = self.thumbnail(cover)
            album.update(count=count, smart=int(bool(album['filter'])), place=int(place), thumbnail=str(preview) if cover and preview.is_file() else '')
            result.append(album)
        result.sort(key=lambda a: (not a['pinned'], a['name'].casefold(), a['id']) if sort == 'name' else
                    (not a['pinned'], -a['created'] if sort == 'newest' else a['created'], a['id']))
        return result

    def details(self, photo_id):
        photo = self.photo(photo_id)
        metadata = json.loads(photo['metadata'])
        from .places import coordinates
        geo = coordinates(metadata)
        from . import live
        parts = live.components(self, photo)
        from . import people
        names=[]
        if self.setting('people_enabled',False) and people.member_visible(self,photo_id):
            names=[r[0] for r in self.db.execute('''SELECT DISTINCT g.name FROM person_photos f JOIN people g ON g.id=f.person_id
                WHERE f.photo_id=? AND g.name<>'' AND g.ignored=0 ORDER BY g.name COLLATE NOCASE,g.id''',(photo_id,))]
        from . import motion
        return dict(id=photo_id, name=photo['name'], live=int(len(parts) == 2 or motion.descriptor(photo) is not None),
                    caption=photo['caption'].encode()[:8192].decode('utf-8', 'ignore'),
                    tags=photo['tags'].encode()[:8192].decode('utf-8', 'ignore'),
                    people=('People: '+' · '.join(names)).encode()[:8192].decode('utf-8','ignore') if names else '',
                    caption_editable=int(len(photo['caption'].encode()) <= 8192),
                    tags_editable=int(len(photo['tags'].encode()) <= 8192),
                    albums=' · '.join(json.loads(photo['albums'])), size=sum(p['size'] for p in parts),
                    width=photo['width'], height=photo['height'],
                    taken=datetime.fromtimestamp(photo['taken'], timezone.utc).isoformat(),
                    location=f"{geo[0]}, {geo[1]}" if geo else '')

    def query(self, view='photos', search='', album='', offset=0, limit=60, sort='newest', album_mode='active', album_sort='name', memory_date='', hidden_access=True, map_bounds=None, map_gallery=False, person='', people_mode='active', lane_year=0, jump_day=''):
        # Map summaries contain coordinates rather than IDs to hydrate. Hold
        # the writer lock until they are returned, so concurrent Hidden/Trash
        # changes cannot overtake the native query and publish private markers.
        locked = view in {'map','people','lane','auto_people'} and not self.db.in_transaction
        if locked:
            self.db.execute('BEGIN IMMEDIATE')
        try:
            return self._query(view, search, album, offset, limit, sort, album_mode, album_sort,
                               memory_date, hidden_access, map_bounds, map_gallery, person, people_mode, lane_year, jump_day)
        finally:
            if locked:
                self.db.rollback()

    def _query(self, view, search, album, offset, limit, sort, album_mode, album_sort, memory_date, hidden_access, map_bounds, map_gallery, person, people_mode, lane_year, jump_day):
        from .query import page
        album_detail = self.db.execute('SELECT * FROM albums WHERE name=? AND deleted=0', (album,)).fetchone() if album else None
        smart_filter = album_detail['filter'] if album_detail else ''
        map_request = dict(map_bounds=map_bounds) if map_bounds is not None else {}
        request = dict(view=view, search=search, album=album, offset=max(0, int(offset)),
                        limit=max(1, min(100, int(limit))), sort=sort, memory_date=memory_date,
                        hidden_access=int(bool(hidden_access)), filter=smart_filter, map_gallery=int(bool(map_gallery)), person=person,
                        lane_year=lane_year,jump_day=jump_day, **map_request)
        if view=='lane':
            from . import people
            detail = people.group(self,person)
            request['birth'] = detail['birth']
            if detail['ignored'] or not self.setting('people_enabled',False): request['person']=''
        if view == 'semantic':
            from .semantic import search_page
            response = search_page(self, request, page)
        elif view=='people' and (not person or not self.setting('people_enabled',False)):
            response = dict(count=0,ids=[])
        else:
            response = page(self, **request)
        count = response['count']
        rows = [self.photo(photo_id) for photo_id in response['ids']] if view != 'map' or map_gallery else []
        photos = []
        for row in rows:
            photo = dict(row)
            # Native ID paging and row hydration are separate reads. A photo
            # hidden by another request in between must not expose its paths.
            if (photo['hidden'] and (not hidden_access or view in {'map', 'semantic','people','lane'})) or (view in {'map', 'semantic','people','lane'} and photo['trashed']):
                continue
            from . import live
            try:
                parts = live.components(self, photo)
            except LibraryError:
                continue
            if any(p['hidden'] and (not hidden_access or view in {'map', 'semantic','people','lane'}) for p in parts):
                continue
            if any(bool(p['trashed']) != bool(photo['trashed']) for p in parts):
                continue
            photo['thumbnail'] = str(self.thumbnail(photo['id'])) if self.thumbnail(photo['id']).exists() else ''
            photo['date'] = datetime.fromtimestamp(photo['taken'], timezone.utc).strftime('%Y-%m-%d')
            photo['original'] = str(self.original(photo)) if self.original(photo).exists() else ''
            from . import decoding
            asset = decoding.display_path(self, photo) if decoding.supported(photo['extension']) else self.original(photo)
            photo['display'] = str(asset) if asset.is_file() else ''
            from . import motion
            photo['live'] = int(bool(json.loads(photo['metadata']).get('photosLive', {}).get('video')) or motion.descriptor(photo) is not None)
            photo['albums'] = json.loads(photo['albums'])
            photos.append(photo)
        albums = self.album_list('places' if view == 'places' else album_mode, album_sort, search if view in {'albums', 'places'} else '')
        totals = self.db.execute('SELECT COUNT(*),COALESCE(SUM(size),0),SUM(dirty) FROM photos').fetchone()
        album_pending = self.db.execute('SELECT COUNT(*) FROM albums WHERE dirty=1').fetchone()[0]
        result = dict(photos=photos, count=count, albums=albums, total=totals[0], bytes=totals[1], pending=(totals[2] or 0) + album_pending,
                    status=self.setting('status', 'Local library · ThinkPad sync only'), importing=self.setting('import', {}))
        if response.get('jump_offset') is not None and jump_day:
            result['jump_offset'] = response['jump_offset']
        if view in {'people','lane'}:
            from . import people
            result['albums'],result['people_count'] = people.catalog(self,search,people_mode,offset) if view=='people' and not person and self.setting('people_enabled',False) else ([],0)
            if person:
                detail = people.group(self,person)
                # Do not publish the identity of a group with no accessible faces.
                result['person_detail'] = detail if photos else dict(id=person,name='',ignored=0)
            if view=='lane':
                result.update(lane_year=response.get('lane_year',0),lane_years=response.get('lane_years',[]),current_year=response.get('current_year',0))
        if view=='auto_people':
            from . import people
            result['photos'],result['count']=[],0
            result['albums'],result['people_count']=people.catalog(self,search,offset=offset,named_only=True) if self.setting('people_enabled',False) else ([],0)
        if view == 'memories':
            result['memory_date'] = response['memory_date']
        if view == 'map':
            result['markers'] = response.get('markers', [])
            result['map_image'] = str(Path(__file__).resolve().parents[1] / 'assets/world.png')
        if album:
            row = self.db.execute('SELECT * FROM albums WHERE name=? AND deleted=0', (album,)).fetchone()
            if row:
                result['album_detail'] = dict(row)
                from .places import is_place
                result['album_detail']['place'] = int(is_place(row['filter']))
        return result

    def export(self, photo_id, folder, _component=False):
        photo = self.photo(photo_id)
        if not _component:
            from . import live
            parts = live.components(self, photo)
            if len(parts) == 2:
                if any(p['hidden'] or p['trashed'] for p in parts):
                    raise LibraryError('Restore and unhide the Live Photo before exporting')
                exported = [self.export(p['id'], folder, _component=True) for p in parts]
                return exported[0]
        source = self.original(photo)
        if not source.exists():
            raise ValueError('Download the original before exporting')
        folder = Path(folder).expanduser().resolve()
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = Path(photo['name']).name
        destination = folder / name
        if destination.exists():
            destination = folder / (Path(name).stem + '-' + photo_id[:12] + photo['extension'])
        if destination.exists():
            raise ValueError('This original is already exported here')
        with source.open('rb') as incoming, destination.open('xb') as output:
            shutil.copyfileobj(incoming, output, 4 * 1024 * 1024)
        metadata = json.loads(photo['metadata'])
        metadata.update(description=photo['caption'], photoTakenTime={'timestamp': str(int(photo['taken']))}, albums=json.loads(photo['albums']))
        metadata.update(tags=photo['tags'], favorite=bool(photo['favorite']), archived=bool(photo['archived']))
        from .people import export_index
        index = export_index(self,photo_id)
        if index is not None: metadata['photosPeople'] = index
        from .auto_albums import export_memberships
        automatic = export_memberships(self,photo)
        if automatic: metadata['photosAutoAlbums'] = automatic
        atomic(folder / 'metadata' / (destination.name + '.json'), packed(metadata).encode())
        return str(destination)
