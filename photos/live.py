"""A Live Photo keeps two immutable originals and one browsable still image."""
import json
from pathlib import Path
import re
import time

from .library import LibraryError, packed

HASH = re.compile(r'^[0-9a-f]{64}$')
STILLS = {'.jpg', '.jpeg', '.heic', '.heif'}
MOVIES = {'.mov', '.mp4'}


def components(library, photo):
    link = json.loads(photo['metadata']).get('photosLive', {})
    if not isinstance(link, dict):
        raise LibraryError('Invalid Live Photo pairing; originals are retained')
    other_id = link.get('video') or link.get('photo')
    if not other_id:
        return [photo]
    if not isinstance(other_id, str) or not HASH.fullmatch(other_id) or other_id == photo['id']:
        raise LibraryError('Invalid Live Photo pairing; originals are retained')
    other = library.photo(other_id)
    still, movie = (photo, other) if link.get('video') else (other, photo)
    if still['kind'] != 'photo' or movie['kind'] != 'video' or \
            json.loads(still['metadata']).get('photosLive') != dict(video=movie['id']) or \
            json.loads(movie['metadata']).get('photosLive') != dict(photo=still['id']):
        raise LibraryError('Live Photo pairing is incomplete; wait for sync or import both originals')
    return [still, movie]


def pair(library, still_id, movie_id):
    with library.db:
        library.db.execute('BEGIN IMMEDIATE')
        still, movie = library.photo(still_id), library.photo(movie_id)
        for photo in [still, movie]:
            if json.loads(photo['metadata']).get('photosLive') or json.loads(photo['metadata']).get('photosLiveSeparated') or json.loads(photo['edits']).get('live_separated'):
                return False
        if still['kind'] != 'photo' or movie['kind'] != 'video':
            return False
        from . import motion
        if motion.descriptor(still) is not None:
            return False
        albums = packed(sorted(set(json.loads(still['albums'])) | set(json.loads(movie['albums']))))
        states = {key: max(still[key], movie[key]) for key in ['hidden', 'trashed', 'archived', 'favorite']}
        for photo, link in [(still, dict(video=movie_id)), (movie, dict(photo=still_id))]:
            metadata = json.loads(photo['metadata'])
            metadata['photosLive'] = link
            library.db.execute('UPDATE photos SET metadata=?,albums=?,hidden=?,trashed=?,archived=?,favorite=?,updated=?,dirty=1 WHERE id=?',
                               (packed(metadata), albums, states['hidden'], states['trashed'], states['archived'],
                                states['favorite'], time.time(), photo['id']))
        return True


def detect(library):
    # Ente exports use one folder and basename for the still and motion file.
    # Ambiguous names, different days and long movies remain independent.
    groups = {}
    for row in library.db.execute('SELECT path,mtime,photo_id FROM sources'):
        path = Path(row['path'])
        if path.suffix.lower() in STILLS | MOVIES:
            groups.setdefault((str(path.parent), path.stem.casefold()), []).append(dict(row))
    from .video import probe
    for entries in groups.values():
        images = {e['photo_id']: e for e in entries if Path(e['path']).suffix.lower() in STILLS}
        movies = {e['photo_id']: e for e in entries if Path(e['path']).suffix.lower() in MOVIES}
        if len(images) != 1 or len(movies) != 1:
            continue
        still_id, movie_id = next(iter(images)), next(iter(movies))
        still, movie = library.photo(still_id), library.photo(movie_id)
        if any(json.loads(p['metadata']).get('photosLive') or json.loads(p['metadata']).get('photosLiveSeparated') or json.loads(p['edits']).get('live_separated') for p in [still, movie]):
            continue
        if abs(still['taken'] - movie['taken']) > 86400 and abs(images[still_id]['mtime'] - movies[movie_id]['mtime']) > 86400 * 1000000000:
            continue
        try:
            info = probe(library.original(movie))
            if info['duration_ms'] <= 10000:
                pair(library, still_id, movie_id)
        except LibraryError:
            continue


def separate(library, photo_id, authorize):
    from . import motion
    if motion.descriptor(library.photo(photo_id)) is not None:
        return motion.separate(library, photo_id, authorize)
    with library.db:
        library.db.execute('BEGIN IMMEDIATE')
        photos = components(library, library.photo(photo_id))
        if len(photos) != 2:
            raise LibraryError('Choose a paired Live Photo')
        if any(not authorize(photo) for photo in photos):
            raise LibraryError('Hidden is locked; authenticate before separating a Live Photo')
        for photo in photos:
            metadata, edits = json.loads(photo['metadata']), json.loads(photo['edits'])
            metadata.pop('photosLive', None)
            metadata['photosLiveSeparated'] = True
            edits['live_separated'] = True
            library.db.execute('UPDATE photos SET metadata=?,edits=?,updated=?,dirty=1 WHERE id=?',
                               (packed(metadata), packed(edits), time.time(), photo['id']))
