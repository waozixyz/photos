"""UI requests and an unattended importer/sync worker share the same WAL store."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import time
import urllib.error

from .library import Library, atomic, packed
from .sync import Client


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


def library_request(library, request, config):
    action = request.get('action', 'query')
    if action == 'query':
        result = library.query(request.get('view', 'photos'), request.get('search', ''),
                               request.get('album', ''), request.get('offset', 0))
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
        sources = list(dict.fromkeys([*config.get('sources', []), *folders]))
        library.set_setting('sources', sources)
        library.set_setting('import_requested', True)
        library.set_setting('status', 'Import queued · originals stay intact')
    elif action == 'sync':
        library.set_setting('sync_requested', True)
        library.set_setting('status', 'ThinkPad sync requested')
    elif action == 'open':
        photo = library.photo(request['id'])
        if not library.original(photo).exists():
            Client(library, config).materialize(photo['id'])
        result = library.query(request.get('view', 'photos'), request.get('search', ''),
                               request.get('album', ''), request.get('offset', 0))
        result['opened'] = request['id']
        return result
    elif action == 'export':
        photo = library.photo(request['id'])
        if not library.original(photo).exists():
            Client(library, config).materialize(photo['id'])
        exported = library.export(request['id'], request.get('value') or str(Path.home() / 'Pictures/Photos Export'))
        library.set_setting('status', 'Original and metadata exported to ' + str(Path(exported).parent))
    else:
        library.edit(request['id'], action, request.get('value'))
    return library.query(request.get('view', 'photos'), request.get('search', ''),
                         request.get('album', ''), request.get('offset', 0))


def daemon(config, once=False):
    library = Library(config['root'])
    lock = (library.root / '.worker.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    last_import = 0
    client = Client(library, config) if config.get('node') else None
    while True:
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
                if pending and not once:
                    continue
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
        if once:
            return
        for _ in range(30):
            if library.setting('sync_requested', False) or library.setting('import_requested', False):
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
