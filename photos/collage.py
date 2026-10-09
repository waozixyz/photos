"""Collage access and immutable-copy adapter; Ziran owns framing and pixels."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from PIL import Image, UnidentifiedImageError

from .library import LibraryError, atomic, packed
from . import decoding, live

HASH = re.compile('[0-9a-f]{64}')


def identities(ids):
    if not isinstance(ids, list) or not 2 <= len(ids) <= 6 or len(set(i for i in ids if isinstance(i, str))) != len(ids):
        raise LibraryError('Select two to six different photos for a collage')
    if any(not isinstance(i, str) or not HASH.fullmatch(i) for i in ids):
        raise LibraryError('Choose valid collage photos')
    return ids


def available(library, photo_id):
    photo = library.photo(photo_id)
    link = json.loads(photo['metadata']).get('photosLive', {})
    if photo['kind'] != 'photo' or (isinstance(link, dict) and link.get('photo')) or any(
            p['hidden'] or p['trashed'] for p in live.components(library, photo)):
        raise LibraryError('Choose visible still photos outside Hidden and Trash for a collage')
    return photo


def prepare(library, ids, config):
    ids = identities(ids)
    for photo_id in ids:
        available(library, photo_id)
    result = []
    for photo_id in ids:
        photo = available(library, photo_id)
        if not library.original(photo).is_file():
            if not config.get('node'):
                raise LibraryError('Download each collage original before creating it')
            from .sync import Client
            Client(library, config).materialize(photo_id)
        photo = available(library, photo_id)
        source = decoding.display(library, photo) if decoding.supported(photo['extension']) else library.original(photo)
        try:
            with Image.open(source) as image:
                if getattr(image, 'n_frames', 1) > 1:
                    raise LibraryError('Choose still images for a collage; animations retain their frames')
                width, height = image.size
                if image.getexif().get(274, 1) in {5, 6, 7, 8}:
                    width, height = height, width
                if not 0 < width * height <= 100000000:
                    raise LibraryError('This collage image exceeds the supported pixel limit')
        except (OSError, ValueError, UnidentifiedImageError):
            raise LibraryError('This photo cannot be decoded for a collage; its original stays safe') from None
        available(library, photo_id)
        result.append(dict(id=photo_id, path=str(source), width=width, height=height))
    for photo_id in ids:
        available(library, photo_id)
    return result


def specification(value):
    try:
        if isinstance(value, str):
            if len(value.encode()) > 8192:
                raise ValueError()
            value = json.loads(value)
        if not isinstance(value, dict) or type(value.get('layout')) is not int or value['layout'] not in range(4):
            raise ValueError()
        if type(value.get('shape')) is not int or value['shape'] not in range(3) or not isinstance(value.get('items'), list):
            raise ValueError()
        ids = identities([item['id'] for item in value['items']])
        items = []
        for item in value['items']:
            if any(type(item.get(key)) is not int or not low <= item[key] <= high
                   for key, low, high in [('cx', 0, 10000), ('cy', 0, 10000), ('zoom', 100, 400)]):
                raise ValueError()
            items.append({key: item[key] for key in ['id', 'cx', 'cy', 'zoom']})
        return dict(layout=value['layout'], shape=value['shape'], items=items)
    except (KeyError, TypeError, ValueError):
        raise LibraryError('Choose a valid collage layout and photo framing') from None


def check(library, value):
    spec = specification(value)
    for item in spec['items']:
        available(library, item['id'])
    return dict(collage_checked=1)


def render(library, spec, sources):
    package = Path(__file__).resolve().parents[1]
    program = next((p for p in [package / 'image_edit', package / 'build/image-editor/image_edit'] if p.is_file()), None)
    if program is None:
        raise LibraryError('Build or install the Photos image processor before making collages')
    payload = {**spec, 'items': [{**item, 'path': source['path']} for item, source in zip(spec['items'], sources)]}
    folder = library.root / 'private/collage'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {k: v for k, v in os.environ.items() if k not in {
        'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'}}
    with tempfile.TemporaryDirectory(prefix='.render-', dir=folder) as temporary:
        output = Path(temporary) / 'collage.png'
        try:
            result = subprocess.run(['prlimit', '--as=1610612736', '--cpu=60', '--', str(program), 'collage', str(output)],
                                    input=packed(payload).encode(), capture_output=True, env=environment, timeout=65)
            if result.returncode:
                raise ValueError()
            with Image.open(output) as image:
                dimensions = image.size
                expected = (1536 if spec['shape'] == 2 else 2048, 1536 if spec['shape'] == 1 else 2048)
                if dimensions != expected or image.format != 'PNG':
                    raise ValueError()
            return output.read_bytes(), dimensions
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise LibraryError('The collage could not finish; your originals stay safe') from None


def save(library, value, config):
    spec = specification(value)
    sources = prepare(library, [item['id'] for item in spec['items']], config)
    data, (width, height) = render(library, spec, sources)
    new_id = hashlib.sha256(data).hexdigest()
    with library.db:
        library.db.execute('BEGIN IMMEDIATE')
        for item in spec['items']:
            available(library, item['id'])
        if library.db.execute('SELECT 1 FROM photos WHERE id=?', (new_id,)).fetchone():
            return new_id
        album = ''
        for number in range(1, 1001):
            candidate = 'Collages' if number == 1 else f'Collages {number}'
            prior = library.db.execute('SELECT deleted,filter FROM albums WHERE name=?', (candidate,)).fetchone()
            if prior is None or (not prior['deleted'] and not prior['filter']):
                album = candidate
                break
        if not album:
            raise LibraryError('Choose another album name before creating more collages')
        destination = library.original(dict(id=new_id, extension='.png'))
        atomic(destination, data)
        now = time.time()
        library.db.execute('''INSERT INTO photos(id,name,extension,kind,size,taken,width,height,metadata,albums,updated)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (new_id, 'Collage.png', '.png', 'photo', len(data), now, width, height,
                                              packed(dict(photosCollage=spec)), packed([album]), now))
        library._ensure_album(album)
    library.preview(library.photo(new_id), destination)
    return new_id
