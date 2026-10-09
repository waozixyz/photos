"""Embedded motion remains inside one immutable, encrypted original."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from .library import LibraryError, digest_file, packed
from .video import environment, probe

EXTENSIONS = {'.jpg', '.jpeg', '.heic', '.heif', '.avif'}
_cache = {}
_guard = threading.Lock()


def descriptor(photo):
    metadata = json.loads(photo['metadata'])
    value = metadata.get('photosMotion')
    if photo['extension'] not in EXTENSIONS or photo['kind'] != 'photo' or metadata.get('photosLive') or metadata.get('photosLiveSeparated') or \
            json.loads(photo['edits']).get('live_separated') or not isinstance(value, dict):
        return None
    if type(value.get('version')) is not int or value['version'] != 1 or any(type(value.get(k)) is not int for k in ['offset', 'length']) or \
            not 16 <= value['length'] <= 128*1024*1024 or not 0 < value['offset'] <= photo['size']-value['length'] or \
            value['offset']+value['length'] > photo['size']:
        return None
    return {key: value[key] for key in ['version', 'offset', 'length']}


def program():
    root = Path(__file__).resolve().parents[1]
    helper = next((p for p in [root/'motion_photo', root/'build/motion-photo/motion_photo'] if p.is_file()), None)
    if helper is None:
        raise LibraryError('Build or install the Motion Photo reader')
    return helper


def read(source, output=None):
    try:
        result = subprocess.run(['prlimit', '--as=268435456', '--cpu=8', '--', str(program()), str(source),
                                 *([str(output)] if output else [])],
                                env=environment(), capture_output=True, timeout=10)
        if result.returncode == 4:
            return None
        if result.returncode or len(result.stdout) > 1024:
            raise ValueError()
        value = json.loads(result.stdout)
        size = source.stat().st_size
        if type(value.get('version')) is not int or value['version'] != 1 or any(type(value.get(k)) is not int for k in ['offset', 'length']) or \
                not 16 <= value['length'] <= 128*1024*1024 or value['offset'] <= 0 or value['offset']+value['length'] > size:
            raise ValueError()
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise LibraryError('Motion Photo could not be read; the original is retained') from None


def detect(library, photo_id, unchanged=False):
    photo = library.photo(photo_id)
    if photo['extension'] not in EXTENSIONS or photo['kind'] != 'photo':
        return
    metadata = json.loads(photo['metadata'])
    if unchanged and library.db.execute('SELECT 1 FROM motion_scans WHERE photo_id=? AND reader_version=3', (photo_id,)).fetchone():
        return
    if metadata.get('photosLive') or metadata.get('photosLiveSeparated') or json.loads(photo['edits']).get('live_separated'):
        return
    value = read(library.original(photo))
    # Imported sidecars cannot invent motion ranges; native parsing is authoritative.
    with library.db:
        library.db.execute('BEGIN IMMEDIATE')
        photo = library.photo(photo_id)
        metadata = json.loads(photo['metadata'])
        if metadata.get('photosLive') or metadata.get('photosLiveSeparated') or json.loads(photo['edits']).get('live_separated'):
            return
        library.db.execute('INSERT OR REPLACE INTO motion_scans VALUES(?,3)', (photo_id,))
        if metadata.get('photosMotion') == value or value is None and 'photosMotion' not in metadata:
            return
        if value is None:
            metadata.pop('photosMotion', None)
        else:
            metadata['photosMotion'] = value
        library.db.execute('UPDATE photos SET metadata=?,updated=?,dirty=1 WHERE id=?',
                           (packed(metadata), time.time(), photo_id))


def check(library, photo_id, authorize):
    photo = library.photo(photo_id)
    if photo['trashed'] or not authorize(photo) or descriptor(photo) is None:
        raise LibraryError('Motion Photo access ended; restore it or unlock Hidden before playing')
    return photo


def extract(library, photo_id, authorize):
    photo = check(library, photo_id, authorize)
    folder = library.root/'private/motion'
    if folder.is_symlink():
        raise LibraryError('Invalid private motion directory')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Playback caches are derived, never sent to backup. Originals stay combined.
    target = folder/(photo_id+'.mp4')
    def fingerprint(path):
        stat = path.stat()
        return (stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
    source = library.original(photo)
    key = (str(library.root),photo_id)
    with _guard:
        cached = _cache.get(key)
        if cached and not target.is_symlink() and target.is_file() and \
                cached[:3] == (descriptor(photo),fingerprint(source),fingerprint(target)):
            check(library, photo_id, authorize)
            return target,cached[3]
    fd, name = tempfile.mkstemp(prefix='.motion-', suffix='.mp4', dir=folder)
    os.close(fd)
    output = Path(name)
    output.unlink()  # The native writer creates exclusively.
    try:
        value = read(library.original(photo), output)
        if value != descriptor(photo) or not output.is_file() or output.stat().st_size != value['length']:
            raise LibraryError('Motion Photo metadata does not match its original')
        # Container validation precedes local codec validation; no network protocols.
        try:
            info = probe(output, motion=True)
        except LibraryError:
            raise LibraryError('The motion clip cannot be played; check FFmpeg codecs and clips from 0.05 to ten seconds') from None
        check(library, photo_id, authorize)
        os.chmod(output, 0o600)
        os.replace(output, target)
        with _guard:
            if len(_cache) >= 256:
                _cache.clear()
            _cache[key] = (value,fingerprint(source),fingerprint(target),info)
        return target, info
    finally:
        output.unlink(missing_ok=True)


def separate(library, photo_id, authorize):
    output, info = extract(library, photo_id, authorize)
    try:
        new_id = digest_file(output)
        with library.db:
            library.db.execute('BEGIN IMMEDIATE')
            photo = check(library, photo_id, authorize)
            metadata = json.loads(photo['metadata'])
            # An identical existing movie retains all of its later edits/privacy.
            if not library.db.execute('SELECT 1 FROM photos WHERE id=?', (new_id,)).fetchone():
                child_metadata = {k:v for k,v in metadata.items() if k not in {'photosMotion','photosMotionScan','photosLive'}}
                child_metadata.update(photosLiveSeparated=True, photosMotionSource=photo_id)
                destination = library.original(dict(id=new_id, extension='.mp4'))
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                import shutil
                fd, name = tempfile.mkstemp(prefix='.motion-copy-', dir=destination.parent)
                temporary = Path(name)
                try:
                    with os.fdopen(fd,'wb') as saved, output.open('rb') as incoming:
                        shutil.copyfileobj(incoming,saved)
                        saved.flush(); os.fsync(saved.fileno())
                    os.replace(temporary,destination)
                finally:
                    temporary.unlink(missing_ok=True)
                edits = json.loads(photo['edits']); edits['live_separated'] = True
                library.db.execute('''INSERT INTO photos(id,name,extension,kind,size,taken,width,height,metadata,albums,
                    favorite,hidden,archived,caption,tags,updated,edits) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (new_id,Path(photo['name']).stem+'-motion.mp4','.mp4','video',output.stat().st_size,
                     photo['taken'],info['width'],info['height'],packed(child_metadata),photo['albums'],
                     photo['favorite'],photo['hidden'],photo['archived'],photo['caption'],photo['tags'],
                     time.time(),packed(edits)))
            metadata['photosLiveSeparated'] = True
            edits = json.loads(photo['edits']); edits['live_separated'] = True
            library.db.execute('UPDATE photos SET metadata=?,edits=?,updated=?,dirty=1 WHERE id=?',
                               (packed(metadata), packed(edits), time.time(), photo_id))
        library.preview(library.photo(new_id), library.original(library.photo(new_id)))
    finally:
        output.unlink(missing_ok=True)
