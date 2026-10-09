"""A merged, content-addressed library. Imported folders are always read-only."""
import hashlib
import json
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
          '.tif', '.tiff', '.bmp', '.avif', '.dng', '.cr2', '.nef', '.arw'}
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
        ''')
        for column, declaration in [('edits', "TEXT NOT NULL DEFAULT '{}'"),
                                    ('archived', 'INTEGER NOT NULL DEFAULT 0')]:
            if column in {row[1] for row in self.db.execute('PRAGMA table_info(photos)')}:
                continue
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                if column not in {row[1] for row in self.db.execute('PRAGMA table_info(photos)')}:
                    self.db.execute(f'ALTER TABLE photos ADD COLUMN {column} {declaration}')
        self.db.commit()

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
            # Unsupported RAW/HEIC files remain in the library without previews.
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
            return 'unchanged'
        photo_id = digest_file(path)
        existing = self.db.execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
        taken, width, height = stat.st_mtime, 0, 0
        if path.suffix.lower() in IMAGES:
            try:
                with Image.open(path) as image:
                    width, height = image.size
                    exif = image.getexif()
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
        album = str(relative.parent) if relative.parent != Path('.') else source_root.name
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
        combined = json.loads(existing['metadata']) if existing else {}
        combined.update(metadata)
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
        return result

    def edit(self, photo_id, action, value=None):
        self.edit_many([photo_id], action, value)

    def edit_many(self, photo_ids, action, value=None):
        if not isinstance(photo_ids, list) or not 0 < len(photo_ids) <= 1000 or any(not isinstance(i, str) for i in photo_ids):
            raise LibraryError('Select between 1 and 1000 photos')
        # Acquire the writer before reading, and roll back the whole selection
        # if one item or value is invalid. Sync must see one complete edit.
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for photo_id in dict.fromkeys(photo_ids):
                self._edit(photo_id, action, value)

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
            if self.db.execute('SELECT 1 FROM photos,json_each(photos.albums) WHERE value=? LIMIT 1', (new,)).fetchone():
                raise LibraryError('An album with that name already exists')
            ids = [r[0] for r in self.db.execute('SELECT id FROM photos WHERE EXISTS(SELECT 1 FROM json_each(photos.albums) WHERE value=?)', (old,))]
            if not ids:
                raise LibraryError('Album not found')
            for photo_id in ids:
                self._edit(photo_id, 'album_remove', old)
                self._edit(photo_id, 'album_add', new)

    def details(self, photo_id):
        photo = self.photo(photo_id)
        metadata = json.loads(photo['metadata'])
        geo = metadata.get('geoDataExif') or metadata.get('geoData') or {}
        return dict(id=photo_id, name=photo['name'],
                    caption=photo['caption'].encode()[:8192].decode('utf-8', 'ignore'),
                    tags=photo['tags'].encode()[:8192].decode('utf-8', 'ignore'),
                    caption_editable=int(len(photo['caption'].encode()) <= 8192),
                    tags_editable=int(len(photo['tags'].encode()) <= 8192),
                    albums=' · '.join(json.loads(photo['albums'])), size=photo['size'],
                    width=photo['width'], height=photo['height'],
                    taken=datetime.fromtimestamp(photo['taken'], timezone.utc).isoformat(),
                    location=f"{geo.get('latitude', '')}, {geo.get('longitude', '')}" if isinstance(geo, dict) and 'latitude' in geo else '')

    def query(self, view='photos', search='', album='', offset=0, limit=60, sort='newest'):
        if sort not in {'newest', 'oldest'}:
            raise LibraryError('Choose newest first or oldest first')
        where, args = ['trashed=0', 'hidden=0'], []
        if view == 'trash':
            where = ['trashed>0']
        elif view == 'hidden':
            where = ['trashed=0', 'hidden=1']
        elif view == 'favorites':
            where.append('favorite=1')
        elif view == 'videos':
            where.append("kind='video'")
        elif view == 'archive':
            where.append('archived=1')
        elif view == 'uncategorized':
            where.append('json_array_length(albums)=0')
        elif view == 'photos' and not album and not search:
            # Archive removes items only from the home timeline. Albums and
            # search retain them, unlike Hidden and Trash.
            where.append('archived=0')
        if album:
            where.append('EXISTS(SELECT 1 FROM json_each(photos.albums) WHERE value=?)')
            args.append(album)
        if search:
            where.append('(name LIKE ? OR caption LIKE ? OR tags LIKE ? OR albums LIKE ? OR strftime(\'%Y-%m-%d\',taken,\'unixepoch\') LIKE ?)')
            args.extend(['%' + search + '%'] * 5)
        clause = ' AND '.join(where)
        count = self.db.execute('SELECT COUNT(*) FROM photos WHERE ' + clause, args).fetchone()[0]
        direction = 'DESC' if sort == 'newest' else 'ASC'
        rows = self.db.execute('SELECT * FROM photos WHERE ' + clause + f' ORDER BY taken {direction},id LIMIT ? OFFSET ?',
                               [*args, max(1, min(100, int(limit))), max(0, int(offset))]).fetchall()
        photos = []
        for row in rows:
            photo = dict(row)
            photo['thumbnail'] = str(self.thumbnail(photo['id'])) if self.thumbnail(photo['id']).exists() else ''
            photo['date'] = datetime.fromtimestamp(photo['taken'], timezone.utc).strftime('%Y-%m-%d')
            photo['original'] = str(self.original(photo)) if self.original(photo).exists() else ''
            photo['albums'] = json.loads(photo['albums'])
            photos.append(photo)
        albums = [dict(name=row[0], count=row[1]) for row in self.db.execute('''
            SELECT value,COUNT(*) FROM photos,json_each(photos.albums)
            WHERE trashed=0 AND hidden=0 GROUP BY value ORDER BY value COLLATE NOCASE''')]
        totals = self.db.execute('SELECT COUNT(*),COALESCE(SUM(size),0),SUM(dirty) FROM photos').fetchone()
        return dict(photos=photos, count=count, albums=albums, total=totals[0], bytes=totals[1], pending=totals[2] or 0,
                    status=self.setting('status', 'Local library · ThinkPad sync only'), importing=self.setting('import', {}))

    def export(self, photo_id, folder):
        photo = self.photo(photo_id)
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
