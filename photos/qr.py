"""On-demand QR payloads remain transient: no disk cache, search index or backup."""
import json
import os
from pathlib import Path
import subprocess
import threading

from .library import LibraryError

_slots = threading.BoundedSemaphore(2)


def program():
    package = Path(__file__).resolve().parents[1]
    candidates = [package / 'detect_qr', package / 'build/detect-qr/detect_qr']
    engine = next((p for p in candidates if p.is_file()), None)
    if engine is None:
        raise LibraryError('Build or install the local QR detection engine')
    return engine


def parse(output):
    try:
        if len(output) > 256 * 1024:
            raise ValueError()
        value = json.loads(output.decode('utf-8'))
        width, height = value['qr_width'], value['qr_height']
        unsupported, codes = value['qr_unsupported'], value['qr_codes']
        if any(type(n) is not int or not 0 < n <= 4096 for n in [width, height]):
            raise ValueError()
        if type(unsupported) is not int or not 0 <= unsupported <= 1024 or not isinstance(codes, list) or len(codes) > 32:
            raise ValueError()
        result, used = [], 0
        for code in codes:
            text = code['text']
            if not isinstance(text, str) or not text or len(text.encode()) > 8192:
                raise ValueError()
            if any((ord(c) < 32 and c not in '\t\r\n') or ord(c) == 127 for c in text):
                raise ValueError()
            x, y, w, h = [code[k] for k in ['x', 'y', 'width', 'height']]
            if any(type(n) is not int for n in [x, y, w, h]):
                raise ValueError()
            if not (0 <= x < width and 0 <= y < height and 0 < w <= width-x and 0 < h <= height-y):
                raise ValueError()
            used += len(text.encode())
            if used > 32768:
                raise ValueError()
            result.append(dict(text=text, x=x*10000//width, y=y*10000//height,
                width=max(1, w*10000//width), height=max(1, h*10000//height)))
        result.sort(key=lambda code: (code['y'], code['x'], code['text']))
        return dict(qr_codes=result, qr_width=width, qr_height=height, qr_unsupported=unsupported)
    except (UnicodeError, ValueError, KeyError, TypeError, OverflowError):
        raise LibraryError('QR detection returned an invalid result; try another photo') from None


def scan(library, photo):
    from . import decoding
    engine = program()
    original = library.original(photo)
    if not original.is_file():
        raise LibraryError('Download the original before detecting QR codes')
    if not _slots.acquire(blocking=False):
        raise LibraryError('QR detection is busy; try again when another scan finishes')
    try:
        source = decoding.display(library, photo) if decoding.supported(photo['extension']) else original
        environment = {k: v for k, v in os.environ.items() if k not in {
            'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'}}
        try:
            result = subprocess.run(['prlimit', '--as=1610612736', '--cpu=20', '--', str(engine), str(source)],
                env=environment, capture_output=True, timeout=25)
        except (OSError, subprocess.TimeoutExpired):
            raise LibraryError('Local QR detection could not finish; your original remains safe') from None
        if result.returncode == 5:
            raise LibraryError('Too many QR contents for one scan; save a cropped copy of the area you need')
        if result.returncode:
            raise LibraryError('QR detection is unavailable for this image; check the installed ZBar codecs')
        return parse(result.stdout)
    finally:
        _slots.release()
