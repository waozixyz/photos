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
from . import guest
from . import app_lock


def configuration():
    path = Path(os.environ.get('PHOTOS_CONFIG', Path.home() / '.config/photos/config.json'))
    if path.is_file():
        return json.loads(path.read_bytes())
    return dict(root=str(Path.home() / '.local/share/photos'), sources=[])


def process(request, config):
    library = Library(config['root'])
    try:
        with guest.guard:
            if guest.scope(library) is not None:
                result = library_request(library, request, config)
                result.update(app_lock.state(library))
                if result.get('guest_exit_authorized') == 1 and app_lock.enabled(library):
                    # Guest exit has authenticated the current app credential.
                    # Return a fresh, separate app session only.
                    token = app_lock.grant(library)
                    result.update(app_lock.state(library, token), app_lock_token=token)
                return result
            from . import recovery
            result = recovery.route(library, request, config)
            if result is not None:
                return result
            result = app_lock.route(library, request)
            if result is not None:
                if result.get('app_lock_authorized') == 1 or (result.get('app_lock_changed') == 1 and not result['app_locked']):
                    gallery = ui_query(library, dict(view='photos', gallery_format=request.get('gallery_format')))
                    gallery.update({key: value for key, value in result.items()
                                    if key not in {'photos', 'albums', 'count', 'total', 'pending'}})
                    if app_lock.state(library, result.get('app_lock_token', ''))['app_locked']:
                        return app_lock.empty(library)
                    return gallery
                return result
            result = library_request(library, request, config)
            if result.get('guest_active') == 1:
                app_lock.revoke(library)
                return result
            state = app_lock.state(library, request.get('app_lock_token', ''))
            # A decode, export or native query may outlast the session. Do not
            # return gallery, original or derived paths after access expires.
            return app_lock.empty(library) if state['app_locked'] else {**result, **state}
    finally:
        library.db.close()


def ui_query(library, request):
    result = library.query(request.get('view', 'photos'), request.get('search', ''),
                           request.get('album', ''), request.get('offset', 0), sort=request.get('sort', 'newest'),
                           album_mode=request.get('album_mode', 'active'), album_sort=request.get('album_sort', 'name'),
                           memory_date=request.get('memory_date', ''), hidden_access=vault.unlocked(library, request.get('vault_token', '')),
                           map_bounds=request.get('map_bounds'), map_gallery=request.get('map_gallery', False),
                           person=request.get('person',''), people_mode=request.get('people_mode','active'),
                           lane_year=request.get('lane_year',0), jump_day=request.get('jump_day',''))
    result.update(vault.state(library, request.get('vault_token', '')))
    result.update(guest_active=0, guest_configured=int(vault.configured(library) or app_lock.enabled(library)))
    # A native page or slow catalog query may outlast the unlock. Recheck
    # before returning any Hidden paths to the caller.
    if not result['vault_unlocked']:
        result['photos'] = [photo for photo in result['photos'] if not photo['hidden']]
        if request.get('view') == 'hidden':
            result['count'] = 0
    # The gallery needs only these fields. Full metadata and encrypted sync
    # manifests stay in the store rather than being decoded on every UI poll.
    modern = request.get('gallery_format') == 2
    for photo in result['photos']:
        if modern and photo['live']:
            photo['kind'] = 'live'
        if not modern:
            # Already open older clients use this field as their image asset.
            # Keep them usable while the signed bundle/service is upgraded.
            photo['original'] = photo['display']
    fields = ('id', 'name', 'thumbnail', 'display' if modern else 'original', 'date', 'kind', 'favorite',
              'hidden', 'trashed', 'archived', 'width', 'height')
    result['photos'] = [{key: photo[key] for key in fields} for photo in result['photos']]
    fields = ('name', 'count', 'thumbnail', 'pinned', 'archived', 'smart', 'place', 'id', 'ignored')
    result['albums'] = [{key: album[key] for key in fields if key in album} for album in result['albums']]
    export = library.setting('export', {})
    result['export_status'] = export.get('status', 'Choose a separate destination for a folder backup')
    result['export_continuous'] = int(bool(export.get('continuous')))
    result['swipe_selection_enabled'] = int(library.setting('swipe_selection_enabled', True) is True)
    from . import semantic
    result.update(semantic.state(library))
    semantic.kick(library)
    from . import people
    result.update(people.state(library))
    people.kick(library)
    return result


def library_request(library, request, config, guest_bypass=False):
    if not guest_bypass:
        response = guest.route(library, request, config)
        if response is not None:
            if response.get('guest_exit_authorized') == 1:
                result = ui_query(library, dict(view='photos', gallery_format=request.get('gallery_format')))
                result.update(response, status='Guest view ended')
                return result
            return response
    action = request.get('action', 'query')
    if action in {'recovery_save', 'recovery_restore'}:
        from . import recovery
        try:
            value = json.loads(request.get('value', ''))
            if not isinstance(value, list) or len(value) != 2 or not all(isinstance(v, str) for v in value):
                raise ValueError()
        except (ValueError, TypeError):
            raise LibraryError('Choose a recovery file and its separate password') from None
        if action == 'recovery_save':
            recovery.save(library, config, request, *value)
            message = 'Encrypted recovery file saved; keep its password separately'
        else:
            message = recovery.restore(library, config, request, *value)
        result = ui_query(library, request); result['status'] = message
        return result
    if action == 'swipe_selection':
        value = request.get('value')
        if not isinstance(value, str) or value not in {'on', 'off'}:
            raise ValueError('Choose on or off for swipe selection')
        library.set_setting('swipe_selection_enabled', value == 'on')
        return ui_query(library, request)
    if action in {'collage_begin', 'collage_check', 'collage_save'}:
        from . import collage
        if action == 'collage_begin':
            return dict(collage_photos=collage.prepare(library, request.get('ids'), config),
                        status='Choose layout, swap photos or drag to frame; Save copy retains originals')
        if action == 'collage_check':
            return collage.check(library, request.get('value'))
        copy_id = collage.save(library, request.get('value'), config)
        memberships = json.loads(library.photo(copy_id)['albums'])
        album = next((name for name in memberships if library.db.execute(
            'SELECT 1 FROM albums WHERE name=? AND deleted=0', (name,)).fetchone()), '')
        result = ui_query(library, {**request, 'view': 'photos', 'album': album, 'search': '', 'offset': 0})
        result.update(saved_copy=copy_id, view='photos', album=album, search='',
                      status='Collage copy retained; originals unchanged')
        return result
    if action in {'person_tag','person_untag'}:
        from . import people
        ids=request.get('ids')
        if ids is None:ids=[request['id']] if request.get('id') else []
        people.tag(library,ids,request.get('value',''),remove=action=='person_untag')
        result=ui_query(library,request)
        if request.get('id') and people.member_visible(library,request['id']):
            detail=library.details(request['id'])
            if people.member_visible(library,request['id']):result['detail']=detail
        result['status']='Person tag removed; detected faces and originals retained' if action=='person_untag' else 'Person tagged; originals retained'
        return result
    if action in {'slide_frame', 'slide_check'}:
        from . import slideshow
        return slideshow.frame(library, request, config)
    if action in {'auto_people_begin','album_auto_people','album_auto_people_remove'}:
        from . import auto_albums
        album = library.album_details(request.get('album',''))
        if album['filter']:
            raise LibraryError('Choose a regular album for automatic people additions')
        if action != 'auto_people_begin':
            auto_albums.configure(library,album['name'],request.get('ids',[]),remove=action=='album_auto_people_remove')
            result = ui_query(library,{**request,'view':'photos','search':'','offset':0})
            result.update(view='photos',search='',album=album['name'])
        else:
            result = ui_query(library,{**request,'view':'auto_people','search':'','offset':0})
            result.update(view='auto_people',search='',album=album['name'],auto_selection=json.loads(album['auto_people']))
        return result
    if action in {'people_enable','people_pause','people_retry','people_disable'}:
        from . import people
        if action=='people_retry':
            with library.db: library.db.execute("DELETE FROM face_scans WHERE error<>''")
        people.enable(library,action in {'people_enable','people_retry'})
        if action=='people_disable': library.set_setting('people_enabled',False)
        return ui_query(library,request)
    if action=='jump_date':
        if not isinstance(request.get('value'),str) or not request['value']:
            raise LibraryError('Choose a gallery date in YYYY-MM-DD format')
        result = ui_query(library,{**request,'jump_day':request.get('value','')})
        result['jumped'] = 1
        return result
    if action in {'person_name','person_ignore','person_remove','person_birth'}:
        from . import people
        ids = request.get('ids') or ([request['id']] if request.get('id') else [])
        person = people.edit(library,action,request.get('person'),request.get('value',''),ids)
        result = ui_query(library,{**request,'person':person,'offset':0})
        result.update(person=person,view=request.get('view','people'),status='People updated; originals retained')
        return result
    if action in {'semantic_enable', 'semantic_pause', 'semantic_retry', 'semantic_disable'}:
        from . import semantic
        if action == 'semantic_disable':
            semantic.enable(library, False)
            library.set_setting('semantic_enabled', False)
        elif action == 'semantic_retry':
            semantic.retry(library)
        else:
            semantic.enable(library, action == 'semantic_enable')
        return ui_query(library, request)
    def authorize(photo):
        return not photo['hidden'] or vault.unlocked(library, request.get('vault_token', ''))
    authorize.deadline = lambda: vault.deadline(library, request.get('vault_token', ''))
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
    if action in {'detect_text', 'text_check', 'detect_qr', 'qr_check'}:
        from . import live, ocr, qr
        qr_mode = action in {'detect_qr', 'qr_check'}
        photo = library.photo(request.get('id'))
        def available():
            fresh = library.photo(photo['id'])
            for part in live.components(library, fresh):
                require_access(part['id'])
                if part['trashed']:
                    raise LibraryError('Restore the photo from Trash before scanning')
            if fresh['kind'] != 'photo':
                raise LibraryError('Choose a photo or Live Photo to scan')
            return fresh
        photo = available()
        if action in {'text_check', 'qr_check'}:
            result = vault.state(library, request.get('vault_token', ''))
            if qr_mode:
                result.update(qr_photo=photo['id'], qr_checked=1)
            else:
                result.update(text_photo=photo['id'], text_checked=1)
            return result
        if not library.original(photo).is_file():
            if not config.get('node'):
                raise LibraryError('Download the original before scanning')
            Client(library, config).materialize(photo['id'])
        photo = available()
        text = qr.scan(library, photo) if qr_mode else ocr.scan(library, photo, request.get('value') or 'eng')
        available()
        result = vault.state(library, request.get('vault_token', ''))
        result.update(text)
        if qr_mode:
            result.update(qr_photo=photo['id'], status='Select a QR code and Copy contents' if text['qr_codes'] else
                ('Detected QR data cannot be displayed as text' if text['qr_unsupported'] else 'No readable QR codes found; try a cropped copy'))
        else:
            result.update(text_photo=photo['id'], text_languages=ocr.languages(), status='Select detected words and Copy selection' if text['text_words'] else 'No readable text found')
        return result
    if action == 'playback_check':
        photo = library.photo(request.get('id'))
        from . import live, motion as embedded_motion
        embedded = embedded_motion.descriptor(photo) is not None
        parts = live.components(library, photo)
        for part in parts:
            require_access(part['id'])
        motion = parts[-1]
        if (motion['kind'] != 'video' and not embedded) or any(p['trashed'] for p in parts):
            raise LibraryError('Choose a video outside Trash to play')
        if not library.original(motion).is_file():
            if not config.get('node'):
                raise LibraryError('Download the original before playing')
            Client(library, config).materialize(motion['id'])
        # Materialization can outlast authentication or a concurrent privacy edit.
        parts = live.components(library, library.photo(photo['id']))
        for part in parts:
            require_access(part['id'])
        if any(p['trashed'] for p in parts):
            raise LibraryError('Restore the video before playing')
        result = vault.state(library, request.get('vault_token', ''))
        if any(library.photo(p['id'])['hidden'] for p in parts) and not result['vault_unlocked']:
            raise LibraryError('Hidden is locked; authenticate before playing hidden videos')
        path = library.original(parts[-1])
        if embedded:
            path, _ = embedded_motion.extract(library, photo['id'], authorize)
            require_access(photo['id'])
        result.update(playback_id=photo['id'], playback_path=str(path))
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
        from . import live
        for part in live.components(library, photo):
            require_access(part['id'])
        if not library.original(photo).exists():
            Client(library, config).materialize(photo['id'])
        require_access(photo['id'])
        for part in live.components(library, library.photo(photo['id'])):
            require_access(part['id'])
        from . import decoding
        if photo['kind'] == 'photo' and decoding.supported(photo['extension']):
            # Older imports without codec thumbnails become viewable on open.
            library.preview(photo, library.original(photo))
            decoding.display(library, photo)
            require_access(photo['id'])
        for part in live.components(library, library.photo(photo['id'])):
            require_access(part['id'])
        result = ui_query(library, request)
        result['opened'] = request['id']
        result['detail'] = library.details(request['id'])
        return result
    elif action == 'live_separate':
        from . import live
        live.separate(library, request.get('id'), authorize)
    elif action in {'place_create', 'place_update'}:
        from . import places
        name = places.create(library, request.get('value'), request.get('id', '')) if action == 'place_create' else places.update(library, request.get('album', ''), request.get('value'))
        request = {**request, 'album': name, 'view': 'photos', 'offset': 0, 'search': '', 'id': ''}
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
            from . import live
            for photo in live.components(library, library.photo(photo_id)):
                require_access(photo['id'])
                if not library.original(photo).exists():
                    Client(library, config).materialize(photo['id'])
                require_access(photo['id'])
            exported = library.export(photo_id, request.get('value') or str(Path.home() / 'Pictures/Photos Export'))
        library.set_setting('status', 'Original and metadata exported to ' + str(Path(exported).parent))
    else:
        library.edit(request['id'], action, request.get('value'), authorize=authorize)
    result = ui_query(library, request)
    if action in {'place_create', 'place_update'}:
        result['place_open'] = 1
    if request.get('id') and action != 'export' and (not library.photo(request['id'])['hidden'] or vault.unlocked(library, request.get('vault_token', ''))):
        result['detail'] = library.details(request['id'])
    if action in {'album_rename', 'album_create', 'album_smart_create', 'album_delete_empty', 'place_create', 'place_update'}:
        result['album'] = request['album']
        result['view'] = request.get('view', 'photos')
        if action in {'album_create', 'album_smart_create', 'place_create', 'place_update'}:
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
        from . import semantic
        semantic.kick(library)
        from . import people
        people.kick(library)
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
