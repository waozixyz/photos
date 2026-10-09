"""Private presentation adapter; the native engine selects and orders slides."""
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import tempfile

from .library import LibraryError
from . import decoding, live, people, query


def selection(library, request):
    source = request.get('view', 'photos')
    if source not in {'photos', 'albums', 'favorites', 'archive', 'uncategorized', 'memories', 'people', 'lane'}:
        raise LibraryError('Choose an album or visible photo gallery for a slideshow')
    offset = request.get('slide_offset', 0)
    seed = request.get('slide_seed', 1)
    if type(offset) is not int or not 0 <= offset <= 2147483647 or type(seed) is not int or not 1 <= seed <= 2147483647:
        raise LibraryError('Choose a valid slideshow position and order')
    search = request.get('search', '')
    if not isinstance(search, str) or len(search.encode()) > 512:
        raise LibraryError('Choose a valid slideshow search')
    names = request.get('slide_albums', [])
    if not isinstance(names, list) or len(names) > 64 or any(not isinstance(n, str) or not n or len(n.encode()) > 512 for n in names):
        raise LibraryError('Choose up to 64 slideshow albums')
    if not names and request.get('album'):
        names = [request['album']]
    result = dict(view='slideshow' if names else source, sort='oldest', search=search,
                  slideshow=1, offset=offset, limit=1, slide_seed=seed,
                  slide_shuffle=int(request.get('slide_shuffle') == 1),
                  memory_date=request.get('memory_date', ''), person=request.get('person', ''))
    if names:
        albums = [library.album_details(name) for name in dict.fromkeys(names)]
        result['slide_albums'] = [dict(name=a['name'], filter=a['filter']) for a in albums]
    elif source == 'albums':
        raise LibraryError('Choose at least one slideshow album')
    if source in {'people', 'lane'}:
        if not library.setting('people_enabled', False) or not result['person']:
            raise LibraryError('Enable People and choose a visible person')
        detail = people.group(library, result['person'])
        if detail['ignored']:
            raise LibraryError('This person is ignored; choose another slideshow')
        if source == 'lane':
            result['birth'] = detail['birth']
    return result


def background(library, photo):
    thumbnail = library.thumbnail(photo['id'])
    if not thumbnail.is_file():
        return ''
    directory = library.root / 'private/slideshow'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = directory / (photo['id'] + '.png')
    if destination.is_file():
        return str(destination)
    package = Path(__file__).resolve().parents[1]
    program = next((p for p in [package / 'image_edit', package / 'build/image-editor/image_edit'] if p.is_file()), None)
    if program is None:
        raise LibraryError('Install the Photos image processor for blurred backgrounds')
    fd, name = tempfile.mkstemp(prefix='.background-', suffix='.png', dir=directory)
    os.close(fd)
    temporary = Path(name)
    environment = {k: v for k, v in os.environ.items() if k not in {
        'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'}}
    try:
        child = subprocess.run(['prlimit', '--as=268435456', '--cpu=10', '--', str(program),
                                'blur', str(thumbnail), str(temporary)], env=environment, capture_output=True, timeout=15)
        if child.returncode:
            raise LibraryError('The slideshow background could not be prepared')
        os.replace(temporary, destination)
        # These replaceable thumbnails are private and bounded across sessions.
        files = sorted(directory.glob('*.png'), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[8:]:
            if old != destination:
                old.unlink(missing_ok=True)
        return str(destination)
    except (OSError, subprocess.TimeoutExpired):
        raise LibraryError('The slideshow background could not finish') from None
    finally:
        temporary.unlink(missing_ok=True)


def frame(library, request, config):
    spec = selection(library, request)
    checking = request.get('action') == 'slide_check'
    photo_id = request.get('id', '')
    if checking:
        if not isinstance(photo_id, str) or len(photo_id) != 64 or any(c not in '0123456789abcdef' for c in photo_id):
            raise LibraryError('Choose a valid slideshow photo')
        spec['slide_id'] = photo_id
    response = query.page(library, **spec)
    if not response['ids']:
        raise LibraryError('No eligible photos remain in this slideshow')
    photo = library.photo(response['ids'][0])
    if not checking:
        if not library.original(photo).is_file():
            if not config.get('node'):
                raise LibraryError('This original is offline; connect to the ThinkPad to continue')
            from .sync import Client
            Client(library, config).materialize(photo['id'])
        asset = decoding.display(library, photo) if decoding.supported(photo['extension']) else library.original(photo)
        backdrop = background(library, photo) if request.get('slide_blur') == 1 else ''
    # Loading or downloading can be slow. Revalidate membership, person rules,
    # Hidden/Trash, Live components and every caption in one writer snapshot.
    library.db.execute('BEGIN IMMEDIATE')
    try:
        fresh_spec = selection(library, request)
        fresh_spec['slide_id'] = photo['id']
        fresh = query.page(library, **fresh_spec)
        if fresh['ids'] != [photo['id']]:
            raise LibraryError('Slideshow access ended; reopen the gallery to continue')
        photo = library.photo(photo['id'])
        parts = live.components(library, photo)
        if photo['kind'] != 'photo' or any(p['hidden'] or p['trashed'] for p in parts):
            raise LibraryError('Slideshow access ended; reopen the gallery to continue')
        result = dict(slide_id=photo['id'], slide_count=fresh['count'], slide_offset=response['slide_offset'],
                      slide_checked=int(checking), slide_name=photo['name'], slide_date=datetime.fromtimestamp(photo['taken'], timezone.utc).strftime('%Y-%m-%d'))
        if not checking:
            result.update(slide_path=str(asset), slide_background=backdrop)
        if request.get('view') == 'lane':
            detail = people.group(library, request['person'])
            year = datetime.fromtimestamp(photo['taken'], timezone.utc).year
            caption = f"{detail['name'] or 'Unnamed person'} · {year}"
            current = datetime.now(timezone.utc).year
            if current > year:
                caption += f' · {current - year} years ago'
            if detail['birth']:
                caption += f" · turned {year - int(detail['birth'][:4])} that year"
            result['slide_caption'] = caption
        else:
            result['slide_caption'] = result['slide_date']
        return result
    finally:
        library.db.rollback()
