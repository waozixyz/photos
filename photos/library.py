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
                trashed INTEGER NOT NULL DEFAULT 0, caption TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '', updated REAL NOT NULL,
                dirty INTEGER NOT NULL DEFAULT 1, manifest TEXT NOT NULL DEFAULT '{}');
            CREATE INDEX IF NOT EXISTS photo_dates ON photos(taken DESC, id);
            CREATE TABLE IF NOT EXISTS sources (
                path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER,
                meta_stamp TEXT, photo_id TEXT REFERENCES photos(id));
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
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
            raise ValueError('Photo not found')
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
        albums = sorted(set(json.loads(existing['albums']) if existing else []) | {album})
        combined = json.loads(existing['metadata']) if existing else {}
        combined.update(metadata)
        if existing:
            if packed(albums) != existing['albums'] or packed(combined) != existing['metadata']:
                corrected = taken if metadata.get('photoTakenTime') else existing['taken']
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
            self.db.execute('''INSERT INTO photos(id,name,extension,kind,size,taken,width,height,metadata,albums,favorite,caption,updated)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (photo_id, path.name, photo['extension'], photo['kind'], stat.st_size, taken,
                 width, height, packed(combined), packed(albums),
                 int(album.casefold() in {'favorites', 'favourites'}), caption, time.time()))
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
        photo = self.photo(photo_id)
        if action in {'favorite', 'hidden'}:
            self.db.execute(f'UPDATE photos SET {action}=?,updated=?,dirty=1 WHERE id=?',
                            (int(not photo[action]), time.time(), photo_id))
        elif action in {'trash', 'restore'}:
            self.db.execute('UPDATE photos SET trashed=?,updated=?,dirty=1 WHERE id=?',
                            (int(time.time()) if action == 'trash' else 0, time.time(), photo_id))
        elif action in {'caption', 'tags'}:
            self.db.execute(f'UPDATE photos SET {action}=?,updated=?,dirty=1 WHERE id=?',
                            (str(value)[:8192], time.time(), photo_id))
        elif action in {'album_add', 'album_remove'}:
            albums = set(json.loads(photo['albums']))
            name = str(value).strip()[:256]
            if not name:
                raise ValueError('Enter an album name')
            albums.add(name) if action == 'album_add' else albums.discard(name)
            self.db.execute('UPDATE photos SET albums=?,updated=?,dirty=1 WHERE id=?',
                            (packed(sorted(albums)), time.time(), photo_id))
        else:
            raise ValueError('Unknown photo action')
        self.db.commit()

    def query(self, view='photos', search='', album='', offset=0, limit=60):
        where, args = ['trashed=0', 'hidden=0'], []
        if view == 'trash':
            where = ['trashed>0']
        elif view == 'hidden':
            where = ['trashed=0', 'hidden=1']
        elif view == 'favorites':
            where.append('favorite=1')
        elif view == 'videos':
            where.append("kind='video'")
        if album:
            where.append('EXISTS(SELECT 1 FROM json_each(photos.albums) WHERE value=?)')
            args.append(album)
        if search:
            where.append('(name LIKE ? OR caption LIKE ? OR tags LIKE ? OR albums LIKE ? OR strftime(\'%Y-%m-%d\',taken,\'unixepoch\') LIKE ?)')
            args.extend(['%' + search + '%'] * 5)
        clause = ' AND '.join(where)
        count = self.db.execute('SELECT COUNT(*) FROM photos WHERE ' + clause, args).fetchone()[0]
        rows = self.db.execute('SELECT * FROM photos WHERE ' + clause + ' ORDER BY taken DESC,id LIMIT ? OFFSET ?',
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
        atomic(folder / 'metadata' / (destination.name + '.json'), packed(metadata).encode())
        return str(destination)
