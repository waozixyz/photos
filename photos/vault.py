"""Hidden gallery authentication; originals and backup keys are never rewritten."""
import fcntl
import json
import os
from pathlib import Path
import secrets
import struct
import subprocess
import threading
import time

from .library import LibraryError, atomic, packed

sessions = {}
failures = {}
guard = threading.Lock()
SESSION_SECONDS = 600


def configured(library):
    return (library.root / 'private/hidden-password.json').is_file()


def unlocked(library, token):
    with guard:
        state = sessions.get(token) if isinstance(token, str) else None
        if state and state[0] == str(library.root) and state[1] > time.monotonic():
            return True
        if state:
            sessions.pop(token, None)
    return False


def lock(library, token):
    with guard:
        if isinstance(token, str) and sessions.get(token, ('',))[0] == str(library.root):
            sessions.pop(token, None)


def state(library, token=''):
    return dict(vault_configured=int(configured(library)), vault_unlocked=int(unlocked(library, token)))


def authenticate(library, password, setup=False):
    if not isinstance(password, str) or len(password) < 8 or len(password.encode()) > 1024 or '\0' in password:
        raise LibraryError('Use at least 8 characters and a password under 1024 bytes')
    package = Path(__file__).resolve().parents[1]
    candidates = [package / 'vault_auth', package / 'build/vault-auth/vault_auth', package / 'vault-auth/vault_auth']
    program = next((path for path in candidates if path.is_file()), None)
    if program is None:
        raise LibraryError('Install the Photos authentication helper before unlocking Hidden')
    private = library.root / 'private'; private.mkdir(exist_ok=True, mode=0o700)
    with (private / '.hidden-password.lock').open('a') as file:
        os.chmod(file.name, 0o600)
        fcntl.flock(file, fcntl.LOCK_EX)
        key = str(library.root)
        now = time.monotonic()
        with guard:
            attempt = failures.get(key, (now, 0))
            if now - attempt[0] >= 60:
                attempt = (now, 0)
            if attempt[1] >= 5:
                raise LibraryError('Too many attempts; wait one minute before trying again')
        path = private / 'hidden-password.json'
        if setup and path.exists():
            raise LibraryError('Hidden already has a password; unlock with the existing password')
        if not setup and not path.is_file():
            raise LibraryError('Create a Hidden password first')
        encoded = password.encode()
        request = struct.pack('!I', len(encoded)) + encoded
        if not setup:
            try:
                verifier = json.loads(path.read_bytes())['verifier']
                if not isinstance(verifier, str) or not verifier.startswith('$argon2id$') or len(verifier) >= 128:
                    raise ValueError()
                request += verifier.encode('ascii')
            except (OSError, ValueError, KeyError):
                raise LibraryError('Hidden password settings need recovery; your originals and backup keys remain intact') from None
        environment = {k: v for k, v in os.environ.items() if k not in {'DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','DBUS_SESSION_BUS_ADDRESS','SESSION_MANAGER'}}
        try:
            result = subprocess.run([str(program), 'hash' if setup else 'verify'], input=request, env=environment,
                                    capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise LibraryError('Hidden authentication is unavailable; try again') from None
        if result.returncode:
            with guard:
                failures[key] = (attempt[0], attempt[1] + 1)
            raise LibraryError('Incorrect password' if not setup and result.returncode == 1 else 'Hidden authentication could not finish')
        if setup:
            verifier = result.stdout.decode('ascii').strip()
            if not verifier.startswith('$argon2id$') or len(verifier) >= 128:
                raise LibraryError('Hidden authentication could not finish')
            atomic(path, packed(dict(verifier=verifier)).encode())
        token = secrets.token_hex(32)
        with guard:
            failures.pop(key, None)
            for existing, entry in list(sessions.items()):
                if entry[1] <= now:
                    sessions.pop(existing, None)
            sessions[token] = (key, time.monotonic() + SESSION_SECONDS)
        return token


def protected(library, request):
    if unlocked(library, request.get('vault_token', '')):
        return False
    ids = request.get('ids')
    if ids is None:
        ids = [request['id']] if request.get('id') else []
    if not isinstance(ids, list) or len(ids) > 1000:
        raise LibraryError('Select at most 1000 photos')
    return any(library.photo(photo_id)['hidden'] for photo_id in ids)
