"""A merged, content-addressed library. Imported folders are always read-only."""
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
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


def exif_location(exif):
    """Read camera GPS without fabricating (0, 0) for absent coordinates."""
    try:
        gps = exif.get_ifd(34853)
        def degrees(value, reference, positive, negative, maximum):
            if reference not in (positive, negative) or len(value) != 3:
                raise ValueError('Invalid GPS coordinate')
            parts = [float(v) for v in value]
            if not all(math.isfinite(v) and v >= 0 for v in parts) or parts[1] >= 60 or parts[2] >= 60:
                raise ValueError('Invalid GPS coordinate')
            coordinate = parts[0] + parts[1] / 60 + parts[2] / 3600
            if coordinate > maximum:
                raise ValueError('Invalid GPS coordinate')
            return -coordinate if reference == negative else coordinate
        return dict(latitude=degrees(gps[2], gps[1], 'N', 'S', 90),
                    longitude=degrees(gps[4], gps[3], 'E', 'W', 180))
    except (AttributeError, KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None


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
            CREATE TABLE IF NOT EXISTS semantic_embeddings (
                photo_id TEXT PRIMARY KEY, model TEXT NOT NULL,
                embedding BLOB, error TEXT NOT NULL DEFAULT '');
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
        self.import_album_names = {}
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
        candidates = [path.parent / 'metadata' / (path.name + '.json'),
                      path.parent / '.meta' / (path.name + '.json'),
                      path.with_name(path.name + '.json')]
        for candidate in candidates:
            if candidate.is_file():
                stat = candidate.stat()
                stamp = f'{candidate}:{stat.st_size}:{stat.st_mtime_ns}'
                if stat.st_size > 4 * 1024 * 1024:
                    raise ValueError('Metadata sidecar is too large')
                value = json.loads(candidate.read_bytes())
                if not isinstance(value, dict):
                    raise ValueError('Metadata sidecar must be an object')
                return value, stamp
        return {}, ''

    def import_file(self, path, source_root):
        path = Path(path)
        if path.is_symlink() or path.suffix.lower() not in IMAGES | VIDEOS:
            return 'skipped'
        stat = path.stat()
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
            return 'unchanged'
        photo_id = digest_file(path)
        existing = self.db.execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
        taken, width, height = stat.st_mtime, 0, 0
        if path.suffix.lower() in IMAGES:
            try:
                with Image.open(path) as image:
                    width, height = image.size
                    exif = image.getexif()
                    gps = exif_location(exif)
                    if gps is not None and 'geoDataExif' not in metadata:
                        metadata['geoDataExif'] = gps
                    date = exif.get(36867) or exif.get(306)
                    if date:
                        taken = datetime.strptime(str(date), '%Y:%m:%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp()
            except (OSError, ValueError, UnidentifiedImageError):
                pass
        try:
            taken = float(metadata.get('photoTakenTime', {}).get('timestamp', taken))
            if not (0 < taken < 32503680000):
                taken = stat.st_mtime
        except (TypeError, ValueError, AttributeError):
            taken = stat.st_mtime
        relative = path.relative_to(source_root)
        album = self.import_album_names.get(str(path.parent), str(relative.parent) if relative.parent != Path('.') else source_root.name)
        if existing:
            # An edit may have completed while EXIF/sidecars were being read.
            # Re-read its markers under the writer lock before merging.
            self.db.execute('BEGIN IMMEDIATE')
            existing = self.db.execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
        edits = json.loads(existing['edits']) if existing else {}
        exported_albums = metadata.get('albums', [])
        if not isinstance(exported_albums, list):
            exported_albums = []
        exported_albums = {name for name in exported_albums if isinstance(name, str) and name.strip() and len(name.encode()) <= 512}
        albums = sorted((set(json.loads(existing['albums']) if existing else []) | {album} | exported_albums)
                        - set(edits.get('removed_albums', [])))
        for name in albums:
            self._ensure_album(name)
        combined = json.loads(existing['metadata']) if existing else {}
        combined.update(metadata)
        if edits.get('live_separated'):
            combined.pop('photosLive', None)
            combined['photosLiveSeparated'] = True
        if 'location' in edits:
            combined.pop('geoData', None)
            combined.pop('geoDataExif', None)
            if edits['location'] is not None:
                combined['geoData'] = edits['location']
        if existing:
            if packed(albums) != existing['albums'] or packed(combined) != existing['metadata']:
                corrected = taken if metadata.get('photoTakenTime') and not edits.get('taken') else existing['taken']
                self.db.execute('UPDATE photos SET albums=?,metadata=?,taken=?,updated=?,dirty=1 WHERE id=?',
                                (packed(albums), packed(combined), corrected, time.time(), photo_id))
        else:
            photo = dict(id=photo_id, extension=path.suffix.lower(), kind='video' if path.suffix.lower() in VIDEOS else 'photo')
            destination = self.original(photo)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix='.import-', dir=destination.parent)
            try:
                with path.open('rb') as source, os.fdopen(fd, 'wb') as output:
                    value = hashlib.sha256()
                    for block in iter(lambda: source.read(4 * 1024 * 1024), b''):
                        value.update(block)
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                if value.hexdigest() != photo_id:
                    raise ValueError('Source changed while importing; retry later')
                os.replace(temporary, destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
            caption = str(metadata.get('description', ''))
            self.db.execute('''INSERT INTO photos(id,name,extension,kind,size,taken,width,height,metadata,albums,favorite,caption,tags,updated,archived)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (photo_id, path.name, photo['extension'], photo['kind'], stat.st_size, taken,
                 width, height, packed(combined), packed(albums),
                 int(album.casefold() in {'favorites', 'favourites'} or metadata.get('favorite') is True), caption,
                 metadata.get('tags', '') if isinstance(metadata.get('tags', ''), str) else '', time.time(),
                 int(metadata.get('archived') is True)))
            self.preview(photo, destination)
        self.db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?)',
                        (str(path), stat.st_size, stat.st_mtime_ns, stamp, photo_id))
        self.db.commit()
        return 'warning' if warning else ('merged' if existing else 'added')

    def import_folders(self, folders):
        result = dict(added=0, merged=0, unchanged=0, skipped=0, warning=0, errors=0)
        failures = []
        for folder in folders:
            root = Path(folder).expanduser().resolve()
            if not root.is_dir() or root == self.root or self.root.is_relative_to(root) or root.is_relative_to(self.root):
                raise ValueError('Choose an existing source folder outside the Photos library')
            for base, directories, filenames in os.walk(root, followlinks=False):
                directories[:] = sorted(d for d in directories if d not in {'metadata', '.meta', '.git'} and not (Path(base) / d).is_symlink())
                album_sidecar = Path(base) / '.photos-album.json'
                if album_sidecar.is_file() and not album_sidecar.is_symlink():
                    try:
                        if album_sidecar.stat().st_size > 16384:
                            raise LibraryError('Album metadata is too large')
                        info = json.loads(album_sidecar.read_bytes())
                        name = self.album_name(info.get('name'))
                        description = info.get('description', '')
                        cover = info.get('cover', '')
                        smart_filter = info.get('filter', '')
                        if not isinstance(smart_filter, str):
                            raise LibraryError('Invalid smart album search')
                        if smart_filter:
                            from .query import validate
                            validate(self, smart_filter)
                        if not isinstance(description, str) or len(description.encode()) > 8192 or not isinstance(cover, str) or cover and (len(cover) != 64 or any(c not in '0123456789abcdef' for c in cover)):
                            raise LibraryError('Invalid album metadata')
                        if any(type(info.get(k, 0)) not in {int, bool} or info.get(k, 0) not in (0, 1) for k in ['pinned', 'archived']):
                            raise LibraryError('Invalid album state')
                        self.import_album_names[str(Path(base))] = name
                        with self.db:
                            self.db.execute('BEGIN IMMEDIATE')
                            existed = self.db.execute('SELECT 1 FROM albums WHERE name=?', (name,)).fetchone()
                            album = self._ensure_album(name)
                            if not existed:
                                self.db.execute('UPDATE albums SET description=?,cover=?,pinned=?,archived=?,filter=? WHERE id=?',
                                                (description, cover, int(info.get('pinned', 0)), int(info.get('archived', 0)), smart_filter, album['id']))
                    except (ValueError, OSError, AttributeError):
                        result['warning'] += 1
                for name in sorted(filenames):
                    try:
                        result[self.import_file(Path(base) / name, root)] += 1
                    except (OSError, ValueError, sqlite3.Error) as error:
                        self.db.rollback()
                        result['errors'] += 1
                        failures.append(dict(path=str(Path(base) / name), error=type(error).__name__))
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
        if not isinstance(photo_ids, list) or not 0 < len(photo_ids) <= 1000 or any(not isinstance(i, str) for i in photo_ids):
            raise LibraryError('Select between 1 and 1000 photos')
        # Acquire the writer before reading, and roll back the whole selection
        # if one item or value is invalid. Sync must see one complete edit.
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            from . import live
            expanded = {}
            for photo_id in dict.fromkeys(photo_ids):
                for component in live.components(self, self.photo(photo_id)):
                    expanded[component['id']] = component
            for photo_id, photo in expanded.items():
                if authorize is not None and not authorize(photo):
                    raise LibraryError('Hidden is locked; authenticate before changing hidden photos')
                component_value = value
                if action == 'rename' and photo['kind'] == 'video' and json.loads(photo['metadata']).get('photosLive'):
                    if not isinstance(value, str) or any(c in value for c in '/\\'):
                        raise LibraryError('Enter a file name without a folder path')
                    component_value = Path(value).stem + photo['extension']
                self._edit(photo_id, action, component_value)

    @staticmethod
    def album_name(value):
        if not isinstance(value, str) or not value.strip() or len(value.strip().encode()) > 512 or any(ord(c) < 32 for c in value):
            raise LibraryError('Enter an album name of at most 512 bytes')
        return value.strip()

    def _edit(self, photo_id, action, value=None):
        photo = self.photo(photo_id)
        edits = json.loads(photo['edits'])
        if action in {'favorite', 'hidden', 'archived', 'favorite_set', 'hidden_set', 'archived_set'}:
            field = action.removesuffix('_set')
            if action.endswith('_set') and str(value) not in {'0', '1'}:
                raise LibraryError('Choose whether to enable this property')
            enabled = int(value) if action.endswith('_set') else int(not photo[field])
            self.db.execute(f'UPDATE photos SET {field}=?,updated=?,dirty=1 WHERE id=?',
                            (enabled, time.time(), photo_id))
        elif action in {'trash', 'restore'}:
            self.db.execute('UPDATE photos SET trashed=?,updated=?,dirty=1 WHERE id=?',
                            (int(time.time()) if action == 'trash' else 0, time.time(), photo_id))
        elif action in {'caption', 'tags'}:
            if not isinstance(value, str) or len(value.encode()) > 8192:
                raise LibraryError('Enter at most 8192 bytes of text')
            self.db.execute(f'UPDATE photos SET {action}=?,updated=?,dirty=1 WHERE id=?',
                            (value, time.time(), photo_id))
        elif action in {'album_add', 'album_remove'}:
            albums = set(json.loads(photo['albums']))
            name = self.album_name(value)
            definition = self.db.execute('SELECT filter FROM albums WHERE name=? AND deleted=0', (name,)).fetchone()
            if definition and definition['filter']:
                raise LibraryError('Smart album membership follows its search; edit the search or photo properties instead')
            if action == 'album_add':
                self._ensure_album(name)
            albums.add(name) if action == 'album_add' else albums.discard(name)
            removed = set(edits.get('removed_albums', []))
            removed.discard(name) if action == 'album_add' else removed.add(name)
            edits['removed_albums'] = sorted(removed)
            self.db.execute('UPDATE photos SET albums=?,edits=?,updated=?,dirty=1 WHERE id=?',
                            (packed(sorted(albums)), packed(edits), time.time(), photo_id))
        elif action == 'taken':
            try:
                date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
                timestamp = (date if date.tzinfo else date.replace(tzinfo=timezone.utc)).timestamp()
                if not 0 < timestamp < 32503680000:
                    raise ValueError()
            except (ValueError, OverflowError):
                raise LibraryError('Enter a valid date and time, for example 2024-06-15 14:30:00+00:00') from None
            edits['taken'] = True
            self.db.execute('UPDATE photos SET taken=?,edits=?,updated=?,dirty=1 WHERE id=?',
                            (timestamp, packed(edits), time.time(), photo_id))
        elif action == 'location':
            if not isinstance(value, str):
                raise LibraryError('Enter latitude, longitude, or leave empty to remove location')
            geo = None
            if value.strip():
                try:
                    parts = value.split(',')
                    if len(parts) != 2:
                        raise ValueError()
                    latitude, longitude = (float(part.strip()) for part in parts)
                    if not math.isfinite(latitude) or not math.isfinite(longitude) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                        raise ValueError()
                    geo = dict(latitude=latitude, longitude=longitude)
                except ValueError:
                    raise LibraryError('Enter valid coordinates: latitude -90 to 90, longitude -180 to 180') from None
            metadata = json.loads(photo['metadata'])
            metadata.pop('geoData', None)
            metadata.pop('geoDataExif', None)
            if geo is not None:
                metadata['geoData'] = geo
            edits['location'] = geo
            self.db.execute('UPDATE photos SET metadata=?,edits=?,updated=?,dirty=1 WHERE id=?',
                            (packed(metadata), packed(edits), time.time(), photo_id))
        elif action == 'rename':
            if not isinstance(value, str) or not value.strip() or value.strip() in {'.', '..'} or any(c in value for c in '/\\') or any(ord(c) < 32 for c in value) or len(value.encode()) > 512:
                raise LibraryError('Enter a file name without a folder path')
            self.db.execute('UPDATE photos SET name=?,updated=?,dirty=1 WHERE id=?',
                            (value.strip(), time.time(), photo_id))
        else:
            raise LibraryError('Unknown photo action')

    def rename_album(self, old, new):
        old, new = self.album_name(old), self.album_name(new)
        if old == new:
            return
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            current = self.album_details(old)
            if self.db.execute('SELECT 1 FROM albums WHERE name=? AND deleted=0', (new,)).fetchone():
                raise LibraryError('An album with that name already exists')
            ids = [r[0] for r in self.db.execute('SELECT id FROM photos WHERE EXISTS(SELECT 1 FROM json_each(photos.albums) WHERE value=?)', (old,))]
            if self.db.execute('SELECT 1 FROM albums WHERE name=? AND deleted=1', (new,)).fetchone():
                raise LibraryError('That album name is retained for synchronization; choose another name')
            self.db.execute('UPDATE albums SET name=?,updated=?,dirty=1 WHERE id=?', (new, time.time(), current['id']))
            for photo_id in ids:
                self._edit(photo_id, 'album_remove', old)
                self._edit(photo_id, 'album_add', new)

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
        if action in {'smart_create', 'filter'}:
            from .query import validate
            search = validate(self, search if action == 'smart_create' else value)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if source:
                photo = self.photo(source)
                if photo['hidden'] or photo['trashed']:
                    raise LibraryError('Choose a visible photo outside Trash to name its place')
            if action in {'create', 'smart_create'}:
                name = self.album_name(value)
                if self.db.execute('SELECT 1 FROM albums WHERE name=?', (name,)).fetchone():
                    raise LibraryError('An album with that name already exists or is retained for synchronization')
                self._ensure_album(name)
                if action == 'smart_create':
                    self.db.execute('UPDATE albums SET filter=? WHERE name=?', (search, name))
                return name
            album = self.album_details(name)
            if action == 'description':
                if not isinstance(value, str) or len(value.encode()) > 8192:
                    raise LibraryError('Enter at most 8192 bytes of text')
                self.db.execute('UPDATE albums SET description=?,updated=?,dirty=1 WHERE id=?', (value, time.time(), album['id']))
            elif action == 'cover':
                if album['filter']:
                    raise LibraryError('Smart albums use the newest matching photo as their cover')
                if value:
                    photo = self.photo(value)
                    if name not in json.loads(photo['albums']) or photo['hidden'] or photo['trashed']:
                        raise LibraryError('Choose a visible photo in this album for its cover')
                self.db.execute('UPDATE albums SET cover=?,updated=?,dirty=1 WHERE id=?', (value or '', time.time(), album['id']))
            elif action in {'pinned', 'archived'}:
                if action == 'archived' and album['filter']:
                    raise LibraryError('Archive individual photos or regular albums; smart albums are saved searches')
                self.db.execute(f'UPDATE albums SET {action}=?,updated=?,dirty=1 WHERE id=?',
                                (int(not album[action]), time.time(), album['id']))
            elif action == 'delete_empty':
                # Hidden and trashed members count as members too. Never detach originals here.
                if not album['filter'] and self.db.execute('SELECT 1 FROM photos,json_each(photos.albums) WHERE value=? LIMIT 1', (name,)).fetchone():
                    raise LibraryError('Only an album with no photos can be deleted')
                self.db.execute('UPDATE albums SET deleted=1,updated=?,dirty=1 WHERE id=?', (time.time(), album['id']))
            elif action == 'filter':
                if not album['filter']:
                    raise LibraryError('Create a smart album to save a search')
                self.db.execute('UPDATE albums SET filter=?,updated=?,dirty=1 WHERE id=?', (search, time.time(), album['id']))
            else:
                raise LibraryError('Unknown album action')
        return name

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
        return dict(id=photo_id, name=photo['name'], live=int(len(parts) == 2),
                    caption=photo['caption'].encode()[:8192].decode('utf-8', 'ignore'),
                    tags=photo['tags'].encode()[:8192].decode('utf-8', 'ignore'),
                    caption_editable=int(len(photo['caption'].encode()) <= 8192),
                    tags_editable=int(len(photo['tags'].encode()) <= 8192),
                    albums=' · '.join(json.loads(photo['albums'])), size=sum(p['size'] for p in parts),
                    width=photo['width'], height=photo['height'],
                    taken=datetime.fromtimestamp(photo['taken'], timezone.utc).isoformat(),
                    location=f"{geo[0]}, {geo[1]}" if geo else '')

    def query(self, view='photos', search='', album='', offset=0, limit=60, sort='newest', album_mode='active', album_sort='name', memory_date='', hidden_access=True, map_bounds=None, map_gallery=False):
        # Map summaries contain coordinates rather than IDs to hydrate. Hold
        # the writer lock until they are returned, so concurrent Hidden/Trash
        # changes cannot overtake the native query and publish private markers.
        locked = view == 'map' and not self.db.in_transaction
        if locked:
            self.db.execute('BEGIN IMMEDIATE')
        try:
            return self._query(view, search, album, offset, limit, sort, album_mode, album_sort,
                               memory_date, hidden_access, map_bounds, map_gallery)
        finally:
            if locked:
                self.db.rollback()

    def _query(self, view, search, album, offset, limit, sort, album_mode, album_sort, memory_date, hidden_access, map_bounds, map_gallery):
        from .query import page
        album_detail = self.db.execute('SELECT * FROM albums WHERE name=? AND deleted=0', (album,)).fetchone() if album else None
        smart_filter = album_detail['filter'] if album_detail else ''
        map_request = dict(map_bounds=map_bounds) if map_bounds is not None else {}
        request = dict(view=view, search=search, album=album, offset=max(0, int(offset)),
                        limit=max(1, min(100, int(limit))), sort=sort, memory_date=memory_date,
                        hidden_access=int(bool(hidden_access)), filter=smart_filter, map_gallery=int(bool(map_gallery)), **map_request)
        if view == 'semantic':
            from .semantic import search_page
            response = search_page(self, request, page)
        else:
            response = page(self, **request)
        count = response['count']
        rows = [self.photo(photo_id) for photo_id in response['ids']] if view != 'map' or map_gallery else []
        photos = []
        for row in rows:
            photo = dict(row)
            # Native ID paging and row hydration are separate reads. A photo
            # hidden by another request in between must not expose its paths.
            if (photo['hidden'] and (not hidden_access or view in {'map', 'semantic'})) or (view in {'map', 'semantic'} and photo['trashed']):
                continue
            from . import live
            try:
                parts = live.components(self, photo)
            except LibraryError:
                continue
            if any(p['hidden'] and (not hidden_access or view in {'map', 'semantic'}) for p in parts):
                continue
            if any(bool(p['trashed']) != bool(photo['trashed']) for p in parts):
                continue
            photo['thumbnail'] = str(self.thumbnail(photo['id'])) if self.thumbnail(photo['id']).exists() else ''
            photo['date'] = datetime.fromtimestamp(photo['taken'], timezone.utc).strftime('%Y-%m-%d')
            photo['original'] = str(self.original(photo)) if self.original(photo).exists() else ''
            from . import decoding
            asset = decoding.display_path(self, photo) if decoding.supported(photo['extension']) else self.original(photo)
            photo['display'] = str(asset) if asset.is_file() else ''
            photo['live'] = int(bool(json.loads(photo['metadata']).get('photosLive', {}).get('video')))
            photo['albums'] = json.loads(photo['albums'])
            photos.append(photo)
        albums = self.album_list('places' if view == 'places' else album_mode, album_sort, search if view in {'albums', 'places'} else '')
        totals = self.db.execute('SELECT COUNT(*),COALESCE(SUM(size),0),SUM(dirty) FROM photos').fetchone()
        album_pending = self.db.execute('SELECT COUNT(*) FROM albums WHERE dirty=1').fetchone()[0]
        result = dict(photos=photos, count=count, albums=albums, total=totals[0], bytes=totals[1], pending=(totals[2] or 0) + album_pending,
                    status=self.setting('status', 'Local library · ThinkPad sync only'), importing=self.setting('import', {}))
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
        atomic(folder / 'metadata' / (destination.name + '.json'), packed(metadata).encode())
        return str(destination)
