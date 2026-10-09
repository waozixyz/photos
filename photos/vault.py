"""Hidden gallery authentication; originals and backup keys are never rewritten."""
import fcntl
import json
import os
from pathlib import Path
import struct
import subprocess
import threading

from .library import LibraryError, atomic, packed
from . import access_sessions

guard = threading.RLock()


def configured(library):
    return (library.root / 'private/hidden-password.json').is_file()


def unlocked(library, token):
    with guard:
        return access_sessions.deadline(library, access_sessions.HIDDEN, token) > 0


def lock(library, token):
    with guard:
        access_sessions.lock(library, access_sessions.HIDDEN, token)


def revoke_all(library):
    with guard:
        access_sessions.revoke(library, access_sessions.HIDDEN)


def deadline(library, token):
    with guard:
        return access_sessions.deadline(library, access_sessions.HIDDEN, token)


def state(library, token=''):
    return dict(vault_configured=int(configured(library)), vault_unlocked=int(unlocked(library, token)))


def password_bytes(password):
    if not isinstance(password, str) or len(password) < 8 or len(password.encode()) > 1024 or '\0' in password:
        raise LibraryError('Use at least 8 characters and a password under 1024 bytes')
    return password.encode()


def verify_password(password, verifier=None, *, pin=False):
    if pin:
        if not isinstance(password, str) or not 4 <= len(password) <= 12 or any(c not in '0123456789' for c in password):
            raise LibraryError('Use an app PIN of 4 through 12 digits')
        encoded = password.encode('ascii')
    else:
        encoded = password_bytes(password)
    package = Path(__file__).resolve().parents[1]
    candidates = [package / 'vault_auth', package / 'build/vault-auth/vault_auth', package / 'vault-auth/vault_auth']
    program = next((path for path in candidates if path.is_file()), None)
    if program is None:
        raise LibraryError('Install the Photos authentication helper before unlocking Hidden')
    request = struct.pack('!I', len(encoded)) + encoded
    if verifier is not None:
        request += verifier.encode('ascii')
    environment = {k: v for k, v in os.environ.items() if k not in {'DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','DBUS_SESSION_BUS_ADDRESS','SESSION_MANAGER'}}
    try:
        mode = ('pin-hash' if verifier is None else 'pin-verify') if pin else ('hash' if verifier is None else 'verify')
        return subprocess.run([str(program), mode], input=request, env=environment,
                              capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise LibraryError('Hidden authentication is unavailable; try again') from None


def hash_password(password, *, pin=False):
    result = verify_password(password, pin=pin)
    if result.returncode:
        raise LibraryError('Hidden authentication could not finish')
    try:
        verifier = result.stdout.decode('ascii').strip()
    except UnicodeError:
        raise LibraryError('Hidden authentication could not finish') from None
    if not verifier.startswith('$argon2id$') or len(verifier) >= 128:
        raise LibraryError('Hidden authentication could not finish')
    return verifier


def change_password(library, token, password):
    password_bytes(password)
    private = library.root / 'private'
    if not configured(library) or not unlocked(library, token):
        raise LibraryError('Unlock Hidden before changing its password')
    with (private / '.hidden-password.lock').open('a') as file:
        os.chmod(file.name, 0o600)
        fcntl.flock(file, fcntl.LOCK_EX)
        verifier = hash_password(password)
        # Password hashing can outlast an unlock. Check and revoke under the
        # same guard so a concurrent lock cannot authorize the replacement.
        with guard:
            if not unlocked(library, token):
                raise LibraryError('Unlock Hidden before changing its password')
            atomic(private / 'hidden-password.json', packed(dict(verifier=verifier)).encode())
            revoke_all(library)
            access_sessions.clear_attempts(library, access_sessions.HIDDEN)


def authenticate(library, password, setup=False):
    password_bytes(password)
    private = library.root / 'private'; private.mkdir(exist_ok=True, mode=0o700)
    with (private / '.hidden-password.lock').open('a') as file:
        os.chmod(file.name, 0o600)
        fcntl.flock(file, fcntl.LOCK_EX)
        with guard:
            access_sessions.check_attempts(library, access_sessions.HIDDEN)
        path = private / 'hidden-password.json'
        if setup and path.exists():
            raise LibraryError('Hidden already has a password; unlock with the existing password')
        if not setup and not path.is_file():
            raise LibraryError('Create a Hidden password first')
        verifier = None
        if not setup:
            try:
                verifier = json.loads(path.read_bytes())['verifier']
                if not isinstance(verifier, str) or not verifier.startswith('$argon2id$') or len(verifier) >= 128:
                    raise ValueError()
                verifier.encode('ascii')
            except (OSError, ValueError, KeyError):
                raise LibraryError('Hidden password settings need recovery; your originals and backup keys remain intact') from None
        result = verify_password(password, verifier)
        if result.returncode:
            with guard:
                access_sessions.failed_attempt(library, access_sessions.HIDDEN)
            raise LibraryError('Incorrect password' if not setup and result.returncode == 1 else 'Hidden authentication could not finish')
        if setup:
            verifier = result.stdout.decode('ascii').strip()
            if not verifier.startswith('$argon2id$') or len(verifier) >= 128:
                raise LibraryError('Hidden authentication could not finish')
            atomic(path, packed(dict(verifier=verifier)).encode())
        with guard:
            access_sessions.clear_attempts(library, access_sessions.HIDDEN)
            return access_sessions.grant(library, access_sessions.HIDDEN)


def protected(library, request):
    if unlocked(library, request.get('vault_token', '')):
        return False
    ids = request.get('ids')
    if ids is None:
        ids = [request['id']] if request.get('id') else []
    if not isinstance(ids, list) or len(ids) > 1000:
        raise LibraryError('Select at most 1000 photos')
    return any(library.photo(photo_id)['hidden'] for photo_id in ids)
