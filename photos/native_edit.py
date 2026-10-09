"""Local authorization adapter for the Ziran SQLite property writer."""
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from .library import LibraryError, packed
from . import vault

ACTIONS = {'favorite', 'hidden', 'archived', 'favorite_set', 'hidden_set',
           'archived_set', 'trash', 'restore', 'caption', 'tags'}
HASH = re.compile(r'^[0-9a-f]{64}$')


def program():
    package = Path(__file__).resolve().parents[1]
    paths = [package/'library_edit', package/'build/library-editor/library_edit']
    result = next((path for path in paths if path.is_file()), None)
    if result is None:
        raise LibraryError('Build or install the Photos property editor before changing photos')
    return result


def change(library, photo_ids, action, value=None, authorize=None):
    if library.db.in_transaction:
        raise LibraryError('Finish the current library operation before editing photos')
    if not isinstance(photo_ids, list) or not 0 < len(photo_ids) <= 1000 or any(not isinstance(i, str) for i in photo_ids):
        raise LibraryError('Select between 1 and 1000 photos')
    if action.endswith('_set'):
        value = str(value)
    try:
        request = packed(dict(ids=photo_ids, action=action, value=value)).encode()
    except (ValueError, TypeError, UnicodeError):
        raise LibraryError('Choose valid photo properties') from None
    if len(request) > 262144:
        raise LibraryError('Photo edit exceeds its limit')
    environment = {key: value for key, value in os.environ.items() if key not in {
        'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'}}
    child = None
    timer = None

    def response():
        line = child.stdout.readline(262145)
        if not line.endswith(b'\n') or len(line) > 262144:
            raise LibraryError('Photo edit outcome could not be confirmed; refresh before changing it again')
        try:
            data = json.loads(line)
        except (ValueError, UnicodeError):
            raise LibraryError('Photo edit returned an invalid response') from None
        if not isinstance(data, dict):
            raise LibraryError('Photo edit returned an invalid response')
        if data.get('error'):
            raise LibraryError(data['error'] if isinstance(data['error'], str) else 'Photo edit could not finish')
        return data

    # Lock/revocation and the native commit have one ordering. The native writer
    # independently checks the actual monotonic expiry through its final commit.
    with vault.guard:
        try:
            child = subprocess.Popen(['prlimit', '--as=268435456', '--cpu=10', '--',
                str(program()), str(library.root/'library.sqlite')], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment)
            timer = threading.Timer(15, child.kill)
            timer.daemon = True; timer.start()
            child.stdin.write(request+b'\n'); child.stdin.flush()
            ids = response().get('authorize')
            if not isinstance(ids, list) or not 0 < len(ids) <= 2000 or \
                    any(not isinstance(i, str) or not HASH.fullmatch(i) for i in ids) or len(set(ids)) != len(ids) or not set(photo_ids).issubset(ids):
                raise LibraryError('Photo edit returned an invalid selection')
            hidden = False
            for photo_id in ids:
                photo = library.photo(photo_id)
                hidden |= bool(photo['hidden'])
                if authorize is not None and not authorize(photo):
                    raise LibraryError('Hidden is locked; authenticate before changing hidden photos')
            until = time.monotonic()+5
            if hidden and authorize is not None and hasattr(authorize, 'deadline'):
                until = min(until, authorize.deadline())
            child.stdin.write(packed(dict(allow=1, until=until)).encode()+b'\n')
            child.stdin.flush()
            result = response()
            if result.get('updated') != len(ids) or child.wait(timeout=5) != 0:
                raise LibraryError('Photo edit outcome could not be confirmed; refresh before changing it again')
        except LibraryError:
            raise
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise LibraryError('Photo edit could not finish; refresh before changing it again') from None
        finally:
            if timer is not None:
                timer.cancel()
            if child is not None:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
                child.stdin.close()
                child.stdout.close()
