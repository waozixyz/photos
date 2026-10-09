"""Raster edits produce new originals; the parent and import sources stay intact."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import time

from PIL import Image, UnidentifiedImageError

from .library import LibraryError, atomic, packed

DEFAULT = [0, 0, 0, 100, 100, 100, 0]
TOKEN = re.compile(r'^[0-9a-f]{32}$')


def options(value):
    try:
        result = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        raise LibraryError('Choose valid image adjustments') from None
    if not isinstance(result, list) or len(result) != 7 or any(type(v) is not int for v in result):
        raise LibraryError('Choose valid image adjustments')
    if result[0] not in (0, 90, 180, 270) or any(v not in (0, 1) for v in result[1:3]) or any(not 0 <= v <= 200 for v in result[3:6]) or result[6] not in (0, 1, 2, 3):
        raise LibraryError('Image adjustments are outside their supported range')
    return result


def render(library, photo_id, adjustments, preview=False):
    photo = library.photo(photo_id)
    if photo['kind'] != 'photo' or photo['trashed']:
        raise LibraryError('Choose a photo outside Trash to edit')
    try:
        with Image.open(library.original(photo)) as source:
            if getattr(source, 'n_frames', 1) > 1:
                raise LibraryError('Animated images cannot be edited without losing frames')
    except (OSError, ValueError, UnidentifiedImageError) as error:
        if isinstance(error, LibraryError):
            raise
        raise LibraryError('This format cannot be edited by the installed image decoder') from None
    # The compatibility service delegates pixels to the native Ziran processor.
    # It does not use a shell, rewrite an original or open a desktop display.
    package = Path(__file__).resolve().parents[1]
    candidates = [package / 'image_edit', package / 'build/image-editor/image_edit', package / 'image-editor/image_edit']
    program = next((path for path in candidates if path.is_file()), None)
    if program is None:
        raise LibraryError('Build or install the Photos image processor before editing')
    temporary_root = library.root / 'private/edit-previews'
    temporary_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.render-', suffix='.png', dir=temporary_root)
    os.close(fd)
    output = Path(name)
    try:
        environment = {key: value for key, value in os.environ.items() if key not in {
            'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'}}
        try:
            result = subprocess.run([str(program), str(library.original(photo)), str(output),
                                     *map(str, adjustments), '1280' if preview else '0'],
                                    env=environment, capture_output=True, timeout=90)
        except (OSError, subprocess.TimeoutExpired):
            raise LibraryError('Image processing could not finish; your original is safe') from None
        if result.returncode:
            raise LibraryError('This image could not be edited by the installed decoder; the original is safe')
        with Image.open(output) as rendered:
            dimensions = rendered.size
        return output.read_bytes(), dimensions
    finally:
        output.unlink(missing_ok=True)


def preview(library, photo_id, value, token=''):
    adjustments = options(value)
    if token and not TOKEN.fullmatch(token):
        raise LibraryError('Invalid image editing session')
    parent = library.root / 'private/edit-previews'
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not token:
        token = secrets.token_hex(16)
        # Remove only abandoned editor previews, never originals or key data.
        for path in parent.iterdir():
            if TOKEN.fullmatch(path.name) and path.is_dir() and not path.is_symlink() and time.time() - path.stat().st_mtime > 86400:
                shutil.rmtree(path)
    folder = parent / token
    if folder.is_symlink():
        raise LibraryError('Invalid image editing session')
    folder.mkdir(exist_ok=True, mode=0o700)
    fingerprint = hashlib.sha256((photo_id + packed(adjustments)).encode()).hexdigest()
    path = folder / (fingerprint + '.png')
    data, dimensions = render(library, photo_id, adjustments, preview=True)
    atomic(path, data)
    # Two distinct asset paths keep the displayed preview valid until the next
    # response and avoid an unbounded folder of slider previews.
    for old in sorted(folder.glob('*.png'), key=lambda p: p.stat().st_mtime, reverse=True)[2:]:
        old.unlink()
    folder.touch()
    return dict(edit_preview=str(path), edit_session=token, edit_width=dimensions[0], edit_height=dimensions[1])


def close(library, token):
    if token and not TOKEN.fullmatch(token):
        raise LibraryError('Invalid image editing session')
    folder = library.root / 'private/edit-previews' / token
    if token and folder.is_dir() and not folder.is_symlink():
        shutil.rmtree(folder)


def save(library, photo_id, value, authorize=None):
    adjustments = options(value)
    if adjustments == DEFAULT:
        raise LibraryError('Make an adjustment before saving a copy')
    data, (width, height) = render(library, photo_id, adjustments)
    new_id = hashlib.sha256(data).hexdigest()
    if new_id == photo_id:
        raise LibraryError('This edit produces the same original')
    with library.db:
        library.db.execute('BEGIN IMMEDIATE')
        photo = library.photo(photo_id)
        if authorize is not None and not authorize(photo):
            raise LibraryError('Hidden is locked; authenticate before saving hidden photos')
        if photo['trashed']:
            raise LibraryError('Restore the photo from Trash before editing')
        existing = library.db.execute('SELECT id FROM photos WHERE id=?', (new_id,)).fetchone()
        if existing:
            # A repeated save never overwrites the previous copy's later edits,
            # privacy state, album membership or recoverable Trash state.
            return new_id
        metadata = json.loads(photo['metadata'])
        metadata['photosEdit'] = dict(parent=photo_id, adjustments=adjustments)
        name = Path(photo['name']).stem.encode()[:480].decode('utf-8', 'ignore') + '-edited.png'
        destination = library.original(dict(id=new_id, extension='.png'))
        atomic(destination, data)
        library.db.execute('''INSERT INTO photos(id,name,extension,kind,size,taken,width,height,metadata,albums,
            favorite,hidden,archived,caption,tags,updated,edits) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (new_id, name, '.png', 'photo', len(data), photo['taken'], width, height, packed(metadata), photo['albums'],
             photo['favorite'], photo['hidden'], photo['archived'], photo['caption'], photo['tags'], time.time(), photo['edits']))
    library.preview(library.photo(new_id), destination)
    return new_id
