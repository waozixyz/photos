"""Native codec display assets are replaceable; original content stays immutable."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from .library import LibraryError

HEIF = {'.heic', '.heif', '.hif', '.avif'}
RAW = {'.dng', '.cr2', '.cr3', '.nef', '.nrw', '.arw', '.sr2', '.srf', '.raf',
       '.rw2', '.rwl', '.orf', '.pef', '.ptx', '.srw', '.3fr', '.fff', '.iiq', '.kdc', '.dcr', '.erf', '.mrw', '.mos', '.mef'}


def supported(extension):
    return extension in HEIF | RAW


def display_path(library, photo):
    return library.root / 'private/display' / (photo['id'] + '.png')


def decode(source, extension, destination, thumbnail=False):
    package = Path(__file__).resolve().parents[1]
    candidates = [package / 'photo_decode', package / 'build/photo-decoder/photo_decode']
    program = next((p for p in candidates if p.is_file()), None)
    if program is None:
        raise LibraryError('Build or install the Photos HEIC/RAW decoder to view this format')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.decode-', suffix='.png', dir=destination.parent)
    os.close(fd)
    temporary = Path(name)
    environment = {key: value for key, value in os.environ.items() if key not in {
        'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'}}
    environment['OMP_NUM_THREADS'] = '2'
    try:
        # prlimit avoids Python preexec_fn in the threaded local service. The
        # child can consume bounded memory/time without touching any display.
        result = subprocess.run(['prlimit', '--as=1610612736', '--cpu=120', '--', str(program),
                                 'heif' if extension in HEIF else 'raw', str(source), str(temporary),
                                 '640' if thumbnail else '0'], env=environment, capture_output=True, timeout=130)
        if result.returncode:
            raise LibraryError('This image could not be decoded by the installed HEIC/RAW codecs; the original is safe')
        dimensions = json.loads(result.stdout)
        if any(type(dimensions.get(k)) is not int or not 0 < dimensions[k] <= 100000 for k in ('width', 'height')):
            raise ValueError('Invalid decoded dimensions')
        os.replace(temporary, destination)
        return dimensions
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise LibraryError('HEIC/RAW decoding could not finish; your original is safe') from None
    finally:
        temporary.unlink(missing_ok=True)


def display(library, photo):
    path = display_path(library, photo)
    if not path.is_file():
        dimensions = decode(library.original(photo), photo['extension'], path)
        library.db.execute('UPDATE photos SET width=?,height=?,updated=?,dirty=1 WHERE id=? AND (width<>? OR height<>?)',
                           (dimensions['width'], dimensions['height'], time.time(), photo['id'], dimensions['width'], dimensions['height']))
        library.db.commit()
    return path
