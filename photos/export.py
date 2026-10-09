"""Resumable one-way export; only unchanged files owned by this export are removed."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .library import LibraryError, digest_file, packed


def destination(library, folder, sources=()):
    if not isinstance(folder, str) or not folder.strip():
        raise LibraryError('Choose an export destination folder')
    root = Path(folder).expanduser().resolve()
    protected = [library.root, *[Path(p).expanduser().resolve() for p in sources],
                 *[Path(p).expanduser().resolve() for p in library.setting('sources', [])]]
    if any(root == p or root.is_relative_to(p) or p.is_relative_to(root) for p in protected):
        raise LibraryError('Export to a separate folder outside your library and import sources')
    return root


def component(value):
    value = ''.join('_' if c in '/\\' or ord(c) < 32 else c for c in value)
    value = value.strip(' .').encode()[:140].decode('utf-8', 'ignore')
    return value or 'Album'


class Export:
    def __init__(self, library, folder, sources=(), materialize=None, cancelled=None):
        self.library = library
        self.root = destination(library, folder, sources)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.materialize = materialize
        self.cancelled = cancelled
        self.lock = None
        self.db = None

    def path(self, relative):
        relative = Path(relative)
        if relative.is_absolute() or '..' in relative.parts:
            raise LibraryError('Invalid export path')
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise LibraryError('Export refuses symbolic links in its destination')
        return current

    def __enter__(self):
        fd = os.open(self.path('.photos-export.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.lock = os.fdopen(fd, 'a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            journal = self.path('.photos-export.sqlite')
            self.db = sqlite3.connect(journal)
            os.chmod(journal, 0o600)
            self.db.row_factory = sqlite3.Row
            self.db.executescript('''CREATE TABLE IF NOT EXISTS owner(root TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, digest TEXT NOT NULL, size INTEGER NOT NULL,
                previous_digest TEXT NOT NULL DEFAULT '', mtime INTEGER NOT NULL DEFAULT 0);''')
            owner = self.db.execute('SELECT root FROM owner').fetchone()
            if owner and owner[0] != str(self.library.root):
                raise LibraryError('This folder belongs to another Photos export')
            if not owner:
                self.db.execute('INSERT INTO owner VALUES(?)', (str(self.library.root),))
                self.db.commit()
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.db:
            self.db.close()
            self.db = None
        if self.lock:
            self.lock.close()
            self.lock = None

    def write(self, relative, data=None, photo=None):
        self.check_cancelled()
        path = self.path(relative)
        row = self.db.execute('SELECT * FROM files WHERE path=?', (relative,)).fetchone()
        expected = photo['id'] if photo else hashlib.sha256(data).hexdigest()
        size = photo['size'] if photo else len(data)
        actual = ''
        if path.exists():
            if not path.is_file() or not row:
                raise LibraryError('Export found an existing file it does not own; choose another folder')
            stat = path.stat()
            if row['digest'] == expected and stat.st_size == size and stat.st_mtime_ns == row['mtime']:
                return False
            actual = digest_file(path)
            if actual == expected and path.stat().st_size == size:
                self.db.execute('UPDATE files SET digest=?,size=?,previous_digest=?,mtime=? WHERE path=?', (expected, size, '', stat.st_mtime_ns, relative))
                self.db.commit()
                return False
            if actual not in {row['digest'], row['previous_digest']}:
                raise LibraryError('An exported file was edited outside Photos; it was preserved')
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix='.photos-next-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as output:
                if photo:
                    source = self.library.original(photo)
                    if not source.is_file() and self.materialize:
                        source = Path(self.materialize(photo['id']))
                    if not source.is_file():
                        raise LibraryError('Export is waiting for an original to download')
                    with source.open('rb') as incoming:
                        shutil.copyfileobj(incoming, output, 4 * 1024 * 1024)
                else:
                    output.write(data)
                output.flush()
                os.fsync(output.fileno())
            if Path(temporary).stat().st_size != size or digest_file(temporary) != expected:
                raise LibraryError('Export original failed its integrity check')
            # Record both versions before publication so an interrupted metadata
            # replacement can resume without treating our own output as foreign.
            self.db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?)',
                            (relative, expected, size, actual, 0))
            self.db.commit()
            if path.exists():
                # Recheck before replacing; never overwrite an outside edit.
                if not row or digest_file(path) not in {row['digest'], row['previous_digest']}:
                    raise LibraryError('An exported file changed during export; it was preserved')
                os.replace(temporary, path)
            else:
                # Exclusive publication also refuses a concurrent foreign file.
                os.link(temporary, path)
            self.db.execute('UPDATE files SET previous_digest=?,mtime=? WHERE path=?', ('', path.stat().st_mtime_ns, relative))
            self.db.commit()
            return True
        finally:
            Path(temporary).unlink(missing_ok=True)

    def check_cancelled(self):
        if self.cancelled and self.cancelled():
            raise LibraryError('Export stopped; your folder copy is retained')

    def run(self):
        # Copy metadata rows before writing files; each pass mirrors one snapshot.
        rows = [dict(p) for p in self.library.db.execute('SELECT * FROM photos WHERE hidden=0 AND trashed=0 ORDER BY id')]
        from . import live
        rows = [p for p in rows if all(not c['hidden'] and not c['trashed'] for c in live.components(self.library, p))]
        albums = {a['name']: a for a in self.library.album_list('all')}
        desired = set()
        written = 0
        folders = {name: component(name) + '--' + a['id'][:12] for name, a in albums.items()}
        for name, album in albums.items():
            relative = folders[name] + '/.photos-album.json'
            desired.add(relative)
            fields = ('name', 'description', 'cover', 'pinned', 'archived', 'filter')
            details = {k: album[k] for k in fields}
            details['auto_people'] = json.loads(album['auto_people'])
            written += self.write(relative, packed(details).encode())
        for photo in rows:
            memberships = [folders[name] for name in json.loads(photo['albums']) if name in folders] or ['Uncategorized']
            name = component(Path(photo['name']).stem) + '--' + photo['id'][:12] + photo['extension']
            metadata = json.loads(photo['metadata'])
            metadata.update(description=photo['caption'], photoTakenTime={'timestamp': str(photo['taken'])},
                            albums=json.loads(photo['albums']), tags=photo['tags'], favorite=bool(photo['favorite']),
                            archived=bool(photo['archived']))
            from .people import export_index
            index = export_index(self.library,photo['id'])
            if index is not None: metadata['photosPeople'] = index
            from .auto_albums import export_memberships
            automatic = export_memberships(self.library,photo)
            if automatic: metadata['photosAutoAlbums'] = automatic
            for folder in memberships:
                relative = folder + '/' + name
                desired.add(relative)
                written += self.write(relative, photo=photo)
                relative = folder + '/metadata/' + name + '.json'
                desired.add(relative)
                written += self.write(relative, packed(metadata).encode())
        removed = 0
        for row in self.db.execute('SELECT * FROM files').fetchall():
            self.check_cancelled()
            if row['path'] in desired:
                continue
            path = self.path(row['path'])
            if path.exists():
                if not path.is_file() or digest_file(path) not in {row['digest'], row['previous_digest']}:
                    raise LibraryError('An old exported file was edited outside Photos; it was preserved')
                path.unlink()
                removed += 1
            self.db.execute('DELETE FROM files WHERE path=?', (row['path'],))
            self.db.commit()
            for parent in [path.parent, path.parent.parent]:
                if parent != self.root and parent.is_relative_to(self.root):
                    try:
                        parent.rmdir()
                    except OSError:
                        pass
        return dict(photos=len(rows), written=written, removed=removed, destination=str(self.root))
