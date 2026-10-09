"""On-demand OCR results stay in bounded memory, outside backup and search."""
from collections import OrderedDict
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from .library import LibraryError, digest_file

_guard = threading.Lock()
_slots = threading.BoundedSemaphore(2)
_cache = OrderedDict()
_verified = {}


def models():
    package = Path(__file__).resolve().parents[1]
    return package / ('models/ocr' if (package / 'models/ocr').is_dir() else 'build/models/ocr')


def languages():
    return sorted(p.stem for p in models().glob('*.traineddata')
                  if re.fullmatch('[a-z_]{2,32}', p.stem) and p.stem != 'osd' and not p.is_symlink())


def prepare(language):
    if not isinstance(language, str) or len(language) > 128 or not re.fullmatch(r'[a-z_]+(?:\+[a-z_]+){0,3}', language):
        raise LibraryError('Choose up to four installed text recognition languages')
    package = Path(__file__).resolve().parents[1]
    candidates = [package / 'detect_text', package / 'build/detect-text/detect_text']
    program = next((p for p in candidates if p.is_file()), None)
    if program is None:
        raise LibraryError('Build or install the local text recognition engine')
    try:
        manifest = json.loads((package / 'assets/TEXT-MODELS.json').read_bytes())
        entries = {p['name']: p for p in manifest['files']}
        for name in language.split('+'):
            path = models() / (name + '.traineddata')
            item = entries[path.name]
            stamp = (str(path), path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_ctime_ns)
            if stamp not in _verified:
                if path.is_symlink() or stamp[1] != item['bytes'] or digest_file(path) != item['sha256']:
                    raise LibraryError('A text recognition model failed its integrity check; reinstall the models')
                _verified[stamp] = True
    except LibraryError:
        raise
    except (OSError, ValueError, KeyError):
        raise LibraryError('Install the selected local text recognition languages before scanning') from None
    return program


def parse(output):
    try:
        header, rows = output.decode('utf-8').split('\n', 1)
        width, height = map(int, header.split('\t'))
        if not 0 < width <= 4096 or not 0 < height <= 4096:
            raise ValueError()
        words, used = [], 0
        for row in rows.splitlines():
            fields = row.split('\t', 11)
            if len(fields) != 12 or fields[0] != '5':
                continue
            left, top, w, h = map(int, fields[6:10])
            confidence = float(fields[10])
            text = fields[11].strip()
            if not math.isfinite(confidence) or not -1 <= confidence <= 100:
                raise ValueError()
            if confidence < 20 or not text:
                continue
            if not (0 <= left < width and 0 <= top < height and 0 < w <= width-left and 0 < h <= height-top):
                raise ValueError()
            if any(ord(c) < 32 or ord(c) == 127 for c in text) or len(text.encode()) > 512:
                raise ValueError()
            used += len(text.encode()) + 1
            if len(words) >= 1024 or used > 32768:
                raise LibraryError('Too much text for one scan; save a cropped copy of the area you need')
            words.append(dict(text=text, line=':'.join(fields[1:5]),
                x=left*10000//width, y=top*10000//height,
                width=max(1,w*10000//width), height=max(1,h*10000//height)))
        return dict(text_words=words, text_width=width, text_height=height)
    except LibraryError:
        raise
    except (UnicodeError, ValueError, OverflowError):
        raise LibraryError('Text recognition returned an invalid result; try another photo') from None


def scan(library, photo, language):
    from . import decoding
    program = prepare(language)
    original = library.original(photo)
    if not original.is_file():
        raise LibraryError('Download the original before detecting text')
    stamp = (str(library.root), photo['id'], language, original.stat().st_mtime_ns,
             program.stat().st_mtime_ns, tuple(_verified))
    # Hidden results are never cached. Authorization is rechecked by the caller
    # after both a cache hit and a slow scan, including every Live component.
    with _guard:
        cached = _cache.get(stamp)
        if cached and not photo['hidden'] and cached[0] > time.monotonic():
            _cache.move_to_end(stamp)
            return cached[1]
    if not _slots.acquire(blocking=False):
        raise LibraryError('Text recognition is busy; try again when another scan finishes')
    try:
        source = decoding.display(library, photo) if decoding.supported(photo['extension']) else original
        environment = {k:v for k,v in os.environ.items() if k not in {
            'DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','DBUS_SESSION_BUS_ADDRESS','SESSION_MANAGER','TESSDATA_PREFIX'}}
        environment['OMP_THREAD_LIMIT'] = '2'
        try:
            result = subprocess.run(['prlimit','--as=1610612736','--cpu=45','--',str(program),
                str(source),str(models()),language],env=environment,capture_output=True,timeout=50)
        except (OSError, subprocess.TimeoutExpired):
            raise LibraryError('Local text recognition could not finish; your original remains safe') from None
        if result.returncode == 5:
            raise LibraryError('Too much text for one scan; save a cropped copy of the area you need')
        if result.returncode:
            raise LibraryError('Text recognition is unavailable for this image; check the installed Tesseract codecs and models')
        value = parse(result.stdout)
        if not photo['hidden']:
            with _guard:
                _cache[stamp] = (time.monotonic()+600, value)
                _cache.move_to_end(stamp)
                while len(_cache) > 32:
                    _cache.popitem(last=False)
        return value
    finally:
        _slots.release()
