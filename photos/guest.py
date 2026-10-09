"""Persistent, read-only presentation scope for the local Photos service."""
from datetime import datetime, timezone
import json
import os
import re
import threading

from .library import LibraryError, atomic, packed
from . import decoding, live, query, vault

# A mode change and every local RPC have one ordering. Background backup still
# uses the library directly and retains its original pinned-node policy.
guard = threading.RLock()
HASH = re.compile('[0-9a-f]{64}')


def path(library):
    return library.root/'private/guest-view.json'


def scope(library):
    record = path(library)
    if not os.path.lexists(record):
        return None
    try:
        if record.is_symlink() or record.stat().st_size > 100000:
            raise ValueError()
        value = json.loads(record.read_bytes())
        if not isinstance(value, dict) or value.get('version') != 1:
            raise ValueError()
        if value.get('kind') == 'selection':
            ids = value['ids']
            if not isinstance(ids, list) or not 0 < len(ids) <= 1000 or any(
                    not isinstance(i, str) or not HASH.fullmatch(i) for i in ids) or len(set(ids)) != len(ids):
                raise ValueError()
        elif value.get('kind') == 'album':
            if not isinstance(value.get('id'), str) or not HASH.fullmatch(value['id']) or not isinstance(value.get('filter'), str):
                raise ValueError()
            if len(value['filter'].encode()) > 512:
                raise ValueError()
        else:
            raise ValueError()
        return value
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        # A damaged marker never grants access to the rest of the library.
        return dict(kind='invalid')


def visible(library, photo_id):
    try:
        photo = library.photo(photo_id)
        parts = live.components(library, photo)
        link = json.loads(photo['metadata']).get('photosLive', {})
        return not link.get('photo') and all(not p['hidden'] and not p['trashed'] for p in parts)
    except (LibraryError, ValueError, TypeError, AttributeError):
        return False


def selected(library, value, offset=0, limit=60):
    if value['kind'] == 'selection':
        ids = [identity for identity in value['ids'] if visible(library, identity)]
        return dict(ids=ids[offset:offset+limit], count=len(ids))
    if value['kind'] == 'album':
        album = library.db.execute('SELECT name FROM albums WHERE id=? AND deleted=0', (value['id'],)).fetchone()
        if album is None:
            return dict(ids=[], count=0)
        # Retain the original smart-album rule even if a separate owner client
        # later edits its definition. A rename follows the stable album ID.
        return query.page(library, view='photos', album=album['name'], filter=value['filter'],
                          hidden_access=0, offset=offset, limit=limit, sort='newest')
    return dict(ids=[], count=0)


def allowed(library, value, photo_id):
    if not isinstance(photo_id, str) or not HASH.fullmatch(photo_id) or not visible(library, photo_id):
        return False
    if value['kind'] == 'selection':
        return photo_id in value['ids']
    if value['kind'] == 'album':
        album = library.db.execute('SELECT name FROM albums WHERE id=? AND deleted=0', (value['id'],)).fetchone()
        if album:
            # Use the same native predicate for the exact item, including smart
            # rules and Live Photo visibility, rather than trusting a client ID.
            result = query.page(library, view='photos', album=album['name'], filter=value['filter'],
                                hidden_access=0, offset=0, limit=1, sort='newest', photo_id=photo_id)
            return photo_id in result['ids']
    return False


def empty(message='Guest view · authenticate to leave'):
    return dict(guest_active=1, vault_configured=1, vault_unlocked=0, vault_token='',
                photos=[], albums=[], count=0, total=0, pending=0, view='photos',
                status=message, guest_configured=1)


def photo(library, identity, modern):
    row = library.photo(identity)
    asset = decoding.display_path(library, row) if decoding.supported(row['extension']) else library.original(row)
    thumbnail = library.thumbnail(identity)
    from . import motion
    paired = bool(json.loads(row['metadata']).get('photosLive', {}).get('video')) or motion.descriptor(row) is not None
    return dict(id=identity, name=row['name'], thumbnail=str(thumbnail) if thumbnail.is_file() else '',
                **{('display' if modern else 'original'): str(asset) if asset.is_file() else ''},
                date=datetime.fromtimestamp(row['taken'], timezone.utc).strftime('%Y-%m-%d'),
                kind='live' if paired and modern else row['kind'], favorite=row['favorite'],
                hidden=0, trashed=0, archived=row['archived'], width=row['width'], height=row['height'])


def page(library, value, request):
    offset = request.get('offset', 0)
    if type(offset) is not int or not 0 <= offset <= 2147483647:
        raise LibraryError('Choose a valid guest page')
    owns_transaction = not library.db.in_transaction
    if owns_transaction:
        library.db.execute('BEGIN IMMEDIATE')
    try:
        selection = selected(library, value, offset)
        result = empty()
        result.update(count=selection['count'], total=selection['count'],
                      photos=[photo(library, identity, request.get('gallery_format') == 2)
                              for identity in selection['ids'] if visible(library, identity)])
        return result
    finally:
        if owns_transaction:
            library.db.rollback()


def start(library, request):
    if not vault.configured(library):
        raise LibraryError('Create a Hidden password first; it protects leaving Guest view')
    if library.db.in_transaction:
        raise LibraryError('Finish the current operation before starting Guest view')
    library.db.execute('BEGIN IMMEDIATE')
    try:
        if request.get('value') == 'album':
            name = request.get('album')
            if not isinstance(name, str) or not name:
                raise LibraryError('Open an album before starting Guest view')
            album = library.album_details(name)
            value = dict(version=1, kind='album', id=album['id'], filter=album['filter'])
            if not selected(library, value)['ids']:
                raise LibraryError('Choose an album with visible photos or videos')
        else:
            ids = request.get('ids')
            if ids is None:
                ids = [request['id']] if request.get('id') else []
            if not isinstance(ids, list) or not 0 < len(ids) <= 1000 or any(
                    not isinstance(i, str) or not HASH.fullmatch(i) for i in ids):
                raise LibraryError('Choose between 1 and 1000 photos or videos for Guest view')
            ids = list(dict.fromkeys(ids))
            if any(not visible(library, identity) for identity in ids):
                raise LibraryError('Restore and unhide every selected item before starting Guest view')
            value = dict(version=1, kind='selection', ids=ids)
        atomic(path(library), packed(value).encode())
        directory = os.open(path(library).parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
        # A prior Hidden token cannot be used to bypass presentation mode.
        with vault.guard:
            for token, entry in list(vault.sessions.items()):
                if entry[0] == str(library.root):
                    vault.sessions.pop(token, None)
    finally:
        library.db.rollback()
    return page(library, value, {**request, 'offset': 0})


def route(library, request, config):
    """Return a scoped response, or None when the owner mode may proceed."""
    value = scope(library)
    action = request.get('action', 'query')
    if value is None:
        return start(library, request) if action == 'guest_start' else None
    try:
        if action == 'guest_exit':
            token = vault.authenticate(library, request.get('value'))
            vault.lock(library, token)
            path(library).unlink()
            # The caller builds a fresh owner gallery only after authentication.
            return dict(guest_active=0, guest_exit_authorized=1)
        if action == 'query':
            return page(library, value, request)
        if action not in {'open', 'playback_check'}:
            raise LibraryError('Guest view permits viewing only; authenticate to leave')
        identity = request.get('id')
        if not allowed(library, value, identity):
            raise LibraryError('This item is outside Guest view or is no longer available')
        if action == 'playback_check':
            # Existing playback rechecks privacy after restoring a remote file;
            # membership is checked again before any decoded path is returned.
            from .worker import library_request
            result = library_request(library, dict(action=action, id=identity), config, guest_bypass=True)
            library.db.execute('BEGIN IMMEDIATE')
            try:
                if not allowed(library, value, identity):
                    raise LibraryError('Guest playback access ended')
                result.update(guest_active=1, vault_unlocked=0, vault_token='')
                return result
            finally: library.db.rollback()
        row = library.photo(identity)
        if row['kind'] == 'photo' and decoding.supported(row['extension']) and library.original(row).is_file():
            decoding.display(library, row)
        library.db.execute('BEGIN IMMEDIATE')
        try:
            if not allowed(library, value, identity):
                raise LibraryError('Guest photo access ended')
            result = page(library, value, request)
            # No album names, people names, locations, private library totals,
            # settings, export destinations or edit capabilities enter this view.
            result['detail'] = dict(id=identity)
            return result
        finally: library.db.rollback()
    except (LibraryError, OSError) as error:
        result = empty()
        result['error'] = str(error) if isinstance(error, LibraryError) else 'Guest view could not finish; authenticate to leave'
        return result
