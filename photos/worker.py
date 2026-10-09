"""UI requests and an unattended importer/sync worker share the same WAL store."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import time
import urllib.error

from .library import Library, LibraryError, atomic, packed
from .sync import Client
from . import vault


def configuration():
    path = Path(os.environ.get('PHOTOS_CONFIG', Path.home() / '.config/photos/config.json'))
    if path.is_file():
        return json.loads(path.read_bytes())
    return dict(root=str(Path.home() / '.local/share/photos'), sources=[])


def process(request, config):
    library = Library(config['root'])
    try:
        return library_request(library, request, config)
    finally:
        library.db.close()


def ui_query(library, request):
    result = library.query(request.get('view', 'photos'), request.get('search', ''),
                           request.get('album', ''), request.get('offset', 0), sort=request.get('sort', 'newest'),
                           album_mode=request.get('album_mode', 'active'), album_sort=request.get('album_sort', 'name'),
                           memory_date=request.get('memory_date', ''), hidden_access=vault.unlocked(library, request.get('vault_token', '')))
    result.update(vault.state(library, request.get('vault_token', '')))
    # A native page or slow catalog query may outlast the unlock. Recheck
    # before returning any Hidden paths to the caller.
    if not result['vault_unlocked']:
        result['photos'] = [photo for photo in result['photos'] if not photo['hidden']]
        if request.get('view') == 'hidden':
            result['count'] = 0
    # The gallery needs only these fields. Full metadata and encrypted sync
    # manifests stay in the store rather than being decoded on every UI poll.
    fields = ('id', 'name', 'thumbnail', 'original', 'date', 'kind', 'favorite',
              'hidden', 'trashed', 'archived', 'width', 'height')
    result['photos'] = [{key: photo[key] for key in fields} for photo in result['photos']]
    fields = ('name', 'count', 'thumbnail', 'pinned', 'archived', 'smart')
    result['albums'] = [{key: album[key] for key in fields} for album in result['albums']]
    export = library.setting('export', {})
    result['export_status'] = export.get('status', 'Choose a separate destination for a folder backup')
    result['export_continuous'] = int(bool(export.get('continuous')))
    return result


def library_request(library, request, config):
    action = request.get('action', 'query')
    def authorize(photo):
        return not photo['hidden'] or vault.unlocked(library, request.get('vault_token', ''))
    def require_access(photo_id):
        if not authorize(library.photo(photo_id)):
            raise LibraryError('Hidden is locked; authenticate before opening or changing hidden photos')
    if action in {'vault_setup', 'vault_unlock'}:
        token = vault.authenticate(library, request.get('value'), setup=action == 'vault_setup')
        request = {**request, 'vault_token': token, 'view': 'hidden', 'offset': 0}
        result = ui_query(library, request)
        result.update(vault_token=token, view='hidden', status='Hidden unlocked')
        return result
    if action == 'vault_lock':
        vault.lock(library, request.get('vault_token', ''))
        from . import editing
        editing.close(library, request.get('edit_session', ''))
        return ui_query(library, {**request, 'vault_token': ''})
    if action == 'vault_change':
        vault.change_password(library, request.get('vault_token', ''), request.get('value'))
        from . import editing
        editing.close(library, request.get('edit_session', ''))
        result = ui_query(library, {**request, 'vault_token': '', 'view': 'hidden', 'offset': 0})
        result.update(vault_token='', view='hidden', status='Password changed; Hidden locked')
        return result
    if action not in {'query', 'import', 'sync', 'memory_date', 'image_close'} and vault.protected(library, request):
        result = ui_query(library, {**request, 'vault_token': ''})
        result.update(error='Hidden is locked; authenticate before opening or changing hidden photos', vault_unlocked=0)
        return result
    if action == 'memory_date':
        request = {**request, 'view': 'memories', 'memory_date': request.get('value', ''), 'offset': 0}
        result = ui_query(library, request)
        result['view'] = 'memories'
        return result
    if action in {'video_status', 'video_cancel'}:
        from . import video, editing
        status = video.state(library, request.get('video_job'), request.get('vault_token', ''), cancel=action == 'video_cancel')
        result = ui_query(library, request)
        result.update(status)
        if status.get('saved_copy'):
            editing.close(library, request.get('edit_session', ''))
        return result
    if action in {'video_preview', 'video_save', 'video_begin'}:
        from . import video, editing
        photo = library.photo(request['id'])
        if not library.original(photo).exists():
            if not config.get('node'):
                raise LibraryError('Download the original before editing')
            Client(library, config).materialize(photo['id'])
        require_access(photo['id'])
        if photo['kind'] != 'video' or photo['trashed']:
            raise LibraryError('Choose a video outside Trash to edit')
        if action == 'video_save':
            token = video.start(library, photo['id'], request.get('value'), request.get('vault_token', ''))
            result = ui_query(library, request)
            result.update(status='Processing video copy; originals stay intact', video_job=token, video_working=1)
            return result
        adjustments = request.get('value')
        if action == 'video_begin':
            adjustments = [0, video.probe(library.original(photo))['duration_ms'], 0, 0]
        preview = video.preview(library, photo['id'], adjustments, request.get('edit_session', ''))
        try:
            require_access(photo['id'])
            if library.photo(photo['id'])['trashed']:
                raise LibraryError('Restore the video from Trash before editing')
        except LibraryError:
            editing.close(library, preview['edit_session'])
            raise
        result = ui_query(library, request)
        result.update(preview)
        return result
    if action in {'image_preview', 'image_save', 'image_close'}:
        from . import editing
        if action == 'image_close':
            editing.close(library, request.get('edit_session', ''))
            return ui_query(library, request)
        photo = library.photo(request['id'])
        if not library.original(photo).exists():
            if not config.get('node'):
                raise LibraryError('Download the original before editing')
            Client(library, config).materialize(photo['id'])
        if action == 'image_preview':
            result = ui_query(library, request)
            result.update(editing.preview(library, photo['id'], request.get('value'), request.get('edit_session', '')))
            try:
                require_access(photo['id'])
            except LibraryError:
                editing.close(library, result['edit_session'])
                raise
            return result
        copy_id = editing.save(library, photo['id'], request.get('value'), authorize=authorize)
        editing.close(library, request.get('edit_session', ''))
        result = ui_query(library, request)
        result['status'] = 'Edited copy saved; original retained'
        result['saved_copy'] = copy_id
        if authorize(library.photo(photo['id'])):
            result['detail'] = library.details(photo['id'])
        return result
    if action == 'query':
        result = ui_query(library, request)
        if config.get('node'):
            client = None
            for photo in result['photos']:
                if not photo['thumbnail'] and json.loads(library.photo(photo['id'])['manifest']).get('preview'):
                    try:
                        client = client or Client(library, config)
                        photo['thumbnail'] = client.materialize(photo['id'], preview_only=True)
                    except (OSError, ValueError, urllib.error.URLError):
                        break
        return result
    if action == 'import':
        folders = request.get('folders') or [request.get('value', '')]
        if not folders or any(not isinstance(folder, str) or not folder.strip() or not Path(folder).expanduser().is_dir() for folder in folders):
            raise ValueError('Choose an existing folder to import')
        exported = library.setting('export', {}).get('destination')
        if exported:
            from .export import destination
            destination(library, exported, [*config.get('sources', []), *folders])
        sources = list(dict.fromkeys([*config.get('sources', []), *library.setting('sources', []), *folders]))
        library.set_setting('sources', sources)
        library.set_setting('import_requested', True)
        library.set_setting('status', 'Import queued · originals stay intact')
    elif action == 'sync':
        library.set_setting('sync_requested', True)
        library.set_setting('status', 'ThinkPad sync requested')
    elif action in {'export_start', 'export_continuous'}:
        from .export import destination
        target = destination(library, request.get('value', ''), config.get('sources', []))
        library.set_setting('export', dict(destination=str(target), continuous=action == 'export_continuous',
                                          requested=True, status='Export queued', revision=time.time_ns()))
    elif action == 'export_stop':
        state = library.setting('export', {})
        state.update(continuous=False, requested=False, status='Continuous export stopped; folder copy retained', revision=time.time_ns())
        library.set_setting('export', state)
    elif action == 'open':
        photo = library.photo(request['id'])
        if not library.original(photo).exists():
            Client(library, config).materialize(photo['id'])
        require_access(photo['id'])
        result = ui_query(library, request)
        result['opened'] = request['id']
        result['detail'] = library.details(request['id'])
        return result
    elif action == 'album_rename':
        library.rename_album(request.get('album'), request.get('value'))
        request = {**request, 'album': request['value'].strip(), 'offset': 0}
    elif action in {'album_create', 'album_smart_create', 'album_filter', 'album_description', 'album_cover', 'album_pinned', 'album_archived', 'album_delete_empty'}:
        name = library.edit_album(request.get('album', ''), action.removeprefix('album_'), request.get('value'), search=request.get('search', ''))
        if action == 'album_delete_empty':
            request = {**request, 'album': '', 'view': 'albums', 'offset': 0}
        elif action in {'album_create', 'album_smart_create'}:
            request = {**request, 'album': name, 'view': 'photos', 'offset': 0, 'search': ''}
    elif request.get('ids') is not None and action != 'export':
        library.edit_many(request['ids'], action, request.get('value'), authorize=authorize)
    elif action == 'export':
        ids = request.get('ids', [request.get('id')])
        if not isinstance(ids, list) or not 0 < len(ids) <= 1000 or any(not isinstance(i, str) for i in ids):
            raise ValueError('Select between 1 and 1000 photos')
        for photo_id in dict.fromkeys(ids):
            photo = library.photo(photo_id)
            if not library.original(photo).exists():
                Client(library, config).materialize(photo['id'])
            require_access(photo['id'])
            exported = library.export(photo_id, request.get('value') or str(Path.home() / 'Pictures/Photos Export'))
        library.set_setting('status', 'Original and metadata exported to ' + str(Path(exported).parent))
    else:
        library.edit(request['id'], action, request.get('value'), authorize=authorize)
    result = ui_query(library, request)
    if request.get('id') and action != 'export' and (not library.photo(request['id'])['hidden'] or vault.unlocked(library, request.get('vault_token', ''))):
        result['detail'] = library.details(request['id'])
    if action in {'album_rename', 'album_create', 'album_smart_create', 'album_delete_empty'}:
        result['album'] = request['album']
        result['view'] = request.get('view', 'photos')
        if action in {'album_create', 'album_smart_create'}:
            result['search'] = ''
    return result


def daemon(config, once=False):
    library = Library(config['root'])
    lock = (library.root / '.worker.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_worker(library, config, once)
    finally:
        lock.close()
        library.db.close()


def run_worker(library, config, once=False):
    last_import = 0
    last_export = 0
    export_retry = 0
    client = Client(library, config) if config.get('node') else None
    while True:
        pending = 0
        try:
            sources = library.setting('sources', config.get('sources', []))
            if sources and (time.monotonic() - last_import > 300 or library.setting('import_requested', False)):
                library.set_setting('status', 'Merging local folders · ThinkPad sync only')
                imported = library.import_folders(sources)
                last_import = time.monotonic()
                library.set_setting('import_requested', False)
                if imported['errors']:
                    library.set_setting('status', f"Import paused: {imported['errors']} files will be retried; source files are safe")
            if config.get('node'):
                pending = client.sync()
                library.set_setting('sync_requested', False)
            else:
                library.set_setting('status', 'Saved locally · configure the ThinkPad node to sync')
        except Exception as error:
            # Never expose HTTP bodies, tokens, signatures or private keys.
            if isinstance(error, urllib.error.HTTPError):
                message = f'ThinkPad sync paused (HTTP {error.code}); originals remain local'
            elif isinstance(error, urllib.error.URLError):
                message = 'ThinkPad offline · originals are safe here; sync retries automatically'
            else:
                message = str(error)[:256]
            library.set_setting('status', message)
            print(message, flush=True)
            if once:
                raise
        state = library.setting('export', {})
        if time.monotonic() >= export_retry and (state.get('requested') or state.get('continuous') and time.monotonic() - last_export > 60):
            try:
                from .export import Export
                state['status'] = 'Exporting library in the background'
                library.set_setting('export', state)
                cancelled = lambda: library.setting('export', {}).get('revision') != state.get('revision')
                with Export(library, state['destination'], config.get('sources', []), client.materialize if client else None, cancelled) as output:
                    result = output.run()
                # Do not overwrite a user's Stop action or changed destination
                # while a large export pass was running.
                latest = library.setting('export', {})
                if latest.get('revision') == state.get('revision'):
                    latest.update(requested=False, status=f"Folder backup current · {result['photos']} photos", result=result)
                    library.set_setting('export', latest)
                last_export = time.monotonic()
            except Exception as error:
                latest = library.setting('export', {})
                if latest.get('revision') == state.get('revision'):
                    latest['status'] = str(error) if isinstance(error, LibraryError) else 'Folder export paused; it will retry without changing your originals'
                    library.set_setting('export', latest)
                    export_retry = time.monotonic() + 30
                if once:
                    raise
        if once:
            return
        if pending:
            continue
        for _ in range(30):
            if library.setting('sync_requested', False) or library.setting('import_requested', False) or library.setting('export', {}).get('requested') and time.monotonic() >= export_retry:
                break
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--daemon', action='store_true')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--import-folders', nargs='+')
    parser.add_argument('request', nargs='?')
    parser.add_argument('response', nargs='?')
    args = parser.parse_args()
    config = configuration()
    if args.import_folders:
        library = Library(config['root'])
        try:
            with (library.root / '.worker.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                print(packed(library.import_folders(args.import_folders)))
        finally:
            library.db.close()
    elif args.daemon or args.once:
        daemon(config, args.once)
    elif args.request and args.response:
        try:
            result = process(json.loads(Path(args.request).read_bytes()), config)
        except Exception as error:
            result = dict(error=f'Local request failed ({type(error).__name__}); your originals are safe')
        atomic(args.response, packed(result).encode())
    else:
        parser.error('Choose daemon, one sync, import, or a UI request')


if __name__ == '__main__':
    main()
