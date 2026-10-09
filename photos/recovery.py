"""Encrypted offline account-key recovery, restricted to the pinned node."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import subprocess
import tempfile

from .library import LibraryError, packed, atomic
from . import account_sign, app_lock, vault, access_sessions, guest

MAGIC = b'PHOTOS-RECOVERY\x01'
LIMIT = 65536
HASH = re.compile('[0-9a-f]{64}')


def program():
    package = Path(__file__).resolve().parents[1]
    path = next((p for p in [package/'recovery_crypto', package/'build/recovery-crypto/recovery_crypto'] if p.is_file()), None)
    if path is None:
        raise LibraryError('Build or install Photos recovery before saving or restoring backup keys')
    return path


def password_bytes(password):
    if not isinstance(password, str) or len(password) < 12 or len(password.encode()) > 1024 or '\0' in password:
        raise LibraryError('Choose at least 12 characters and a recovery password under 1024 bytes')
    return password.encode()


def crypt(password, payload, *, restore=False):
    secret = password_bytes(password)
    if not isinstance(payload, bytes) or (restore and not 57 <= len(payload) <= LIMIT+56) or (not restore and not 1 <= len(payload) <= LIMIT):
        raise LibraryError('Invalid Photos recovery file')
    salt_nonce, data = (payload[:40], payload[40:]) if restore else (bytes(40), payload)
    frame = struct.pack('!BBHI', 1, 2 if restore else 1, len(secret), len(data))+salt_nonce+secret+data
    environment = {k: v for k, v in os.environ.items() if k not in {
        'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'}}
    try:
        result = subprocess.run(['prlimit', '--core=0', '--as=536870912', '--cpu=60', '--', str(program())],
                                input=frame, capture_output=True, env=environment, timeout=65)
    except (OSError, subprocess.TimeoutExpired):
        raise LibraryError('Recovery could not finish; existing originals and keys were preserved') from None
    if result.returncode == 1 and restore:
        raise LibraryError('Incorrect recovery password or altered recovery file')
    expected = len(data)-16 if restore else len(data)+56
    if result.returncode or result.stderr or len(result.stdout) != expected:
        raise LibraryError('Recovery could not finish; existing originals and keys were preserved')
    return result.stdout


def identity(value):
    try:
        if type(value) is not dict or set(value) != {'public', 'private', 'content_key'}:
            raise ValueError()
        public, private, content = (bytes.fromhex(value[k]) for k in ['public', 'private', 'content_key'])
        if len(public) != account_sign.PUBLIC_BYTES or len(private) != account_sign.PRIVATE_BYTES or len(content) != 32:
            raise ValueError()
        challenge = b'photos-recovery-identity-v1\n'+secrets.token_bytes(32)
        if not account_sign.verify(public, challenge, account_sign.sign(private, challenge)):
            raise ValueError()
        return hashlib.sha256(public).hexdigest()
    except (ValueError, TypeError, KeyError):
        raise LibraryError('Invalid backup identity; existing keys were preserved') from None


def file_path(value, library):
    if not isinstance(value, str) or not value or len(value.encode()) > 4096 or '\0' in value:
        raise LibraryError('Choose a full path for a separate recovery file')
    path = Path(value).expanduser()
    if not path.is_absolute() or path.is_symlink():
        raise LibraryError('Choose a full path for a separate recovery file')
    path = path.resolve()
    if path.is_relative_to(library.root):
        raise LibraryError('Keep the recovery file outside your photo library')
    return path


def permitted(library, request):
    if app_lock.state(library, request.get('app_lock_token', ''))['app_locked']:
        raise LibraryError('Unlock Photos before saving recovery keys')
    if vault.configured(library) and not vault.unlocked(library, request.get('vault_token', '')):
        raise LibraryError('Unlock Hidden before saving recovery keys')


def save(library, config, request, destination, password):
    password_bytes(password)
    permitted(library, request)
    path = file_path(destination, library)
    if not path.parent.is_dir() or path.exists():
        raise LibraryError('Choose a new recovery file in an existing folder')
    pin = config.get('node_id')
    if not isinstance(pin, str) or not HASH.fullmatch(pin):
        raise LibraryError('Pin the private ThinkPad node before saving recovery keys')
    marker = library.root/'private/identity.json'
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > LIMIT:
        raise LibraryError('Existing backup keys are unavailable; no keys were replaced')
    before = marker.read_bytes()
    try:
        value = json.loads(before)
    except (ValueError, UnicodeError):
        raise LibraryError('Existing backup keys are invalid; no keys were replaced') from None
    account = identity(value)
    data = MAGIC+crypt(password, packed(dict(version=1, node_id=pin, account=account, identity=value)).encode())
    # Slow native password derivation must never outlast permission to export.
    permitted(library, request)
    if marker.read_bytes() != before:
        raise LibraryError('Backup identity changed; try saving recovery again')
    descriptor, temporary = tempfile.mkstemp(prefix='.photos-recovery-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data); output.flush(); os.fsync(output.fileno())
        with vault.guard:
            permitted(library, request)
            os.link(temporary, path)  # Exclusive publication; never overwrite a file.
    except FileExistsError:
        raise LibraryError('The recovery destination already exists; it was preserved') from None
    finally:
        Path(temporary).unlink(missing_ok=True)
    return str(path)


def read(library, config, source, password):
    """Verify the encrypted proof without changing identity or granting access."""
    path = file_path(source, library)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > len(MAGIC)+LIMIT+56:
                raise LibraryError('Choose a valid Photos recovery file')
            data = file.read(len(MAGIC)+LIMIT+57)
    except OSError:
        raise LibraryError('Choose a valid Photos recovery file') from None
    if len(data) > len(MAGIC)+LIMIT+56:
        raise LibraryError('Choose a valid Photos recovery file')
    if not data.startswith(MAGIC):
        raise LibraryError('Choose a valid Photos recovery file')
    try:
        value = json.loads(crypt(password, data[len(MAGIC):], restore=True))
        if (type(value) is not dict or set(value) != {'version', 'node_id', 'account', 'identity'}
                or type(value['version']) is not int or value['version'] != 1
                or not isinstance(value['node_id'], str) or not HASH.fullmatch(value['node_id'])
                or value['node_id'] != config.get('node_id') or identity(value['identity']) != value['account']):
            raise ValueError()
    except LibraryError:
        raise
    except (ValueError, KeyError, TypeError, RecursionError):
        raise LibraryError('Recovery identity or ThinkPad pin is invalid; existing keys were preserved') from None
    return value


def restore(library, config, request, source, password):
    password_bytes(password)
    # Restoring keys is initial setup; credential reset is separately scoped.
    permitted(library, request)
    value = read(library, config, source, password)
    private = library.root/'private'; private.mkdir(exist_ok=True, mode=0o700)
    lock = (library.root/'.worker.lock').open('a')
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LibraryError('Stop background backup before restoring keys into an empty library') from None
        permitted(library, request)
        library.db.execute('BEGIN IMMEDIATE')
        try:
            if (library.db.execute('SELECT count(*) FROM photos').fetchone()[0]
                    or library.db.execute('SELECT count(*) FROM albums').fetchone()[0]
                    or os.path.lexists(private/'identity.json') or os.path.lexists(private/'device.json')
                    or library.setting('server_version', 0) or library.setting('record_cursor')):
                raise LibraryError('Restore into a new empty library; existing photos and keys were preserved')
            descriptor, temporary = tempfile.mkstemp(prefix='.recovery-', dir=private)
            try:
                with os.fdopen(descriptor, 'wb') as output:
                    output.write(packed(value['identity']).encode()); output.flush(); os.fsync(output.fileno())
                with vault.guard:
                    permitted(library, request)
                    os.link(temporary, private/'identity.json')
            finally:
                Path(temporary).unlink(missing_ok=True)
        finally:
            library.db.rollback()  # No library records or progress were modified.
    finally:
        lock.close()
    return 'Backup keys restored; reconnect to the pinned ThinkPad to restore your photos'


def reset(library, config, source, password, replacement, kind):
    """Replace one local verifier only after proof of this exact backup identity."""
    if kind not in {'app', 'hidden'}:
        raise LibraryError('Choose App lock or Hidden recovery')
    vault.password_bytes(replacement)
    scope = access_sessions.APP if kind == 'app' else access_sessions.HIDDEN
    with guest.guard:
        if guest.scope(library) is not None:
            raise LibraryError('Leave Guest view before recovering local credentials')
        access_sessions.check_attempts(library, scope)
        try:
            proof = read(library, config, source, password)
            marker = library.root/'private/identity.json'
            if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > LIMIT:
                raise LibraryError('Existing backup keys are unavailable; no keys were replaced')
            before = marker.read_bytes()
            if json.loads(before) != proof['identity']:
                raise LibraryError('Recovery file belongs to different backup keys; existing keys were preserved')
            private = marker.parent
            target = app_lock.path(library) if kind == 'app' else private/'hidden-password.json'
            lock_path = private/('.app-lock.lock' if kind == 'app' else '.hidden-password.lock')
            with app_lock.credential_lock(lock_path):
                if not os.path.lexists(target):
                    raise LibraryError('The selected local lock is not configured')
                if target.is_symlink() or not target.is_file() or target.stat().st_size > 1024:
                    raise LibraryError('Local password settings cannot be replaced safely')
                old = target.read_bytes()
                config_value = app_lock.settings(library) if kind == 'app' else None
                if kind == 'app' and not config_value['enabled']:
                    raise LibraryError('App lock is not enabled')
                verifier = vault.hash_password(replacement)
                with vault.guard:
                    if (guest.scope(library) is not None or marker.is_symlink() or marker.read_bytes() != before
                            or target.is_symlink() or target.read_bytes() != old):
                        raise LibraryError('Credentials or backup identity changed; try recovery again')
                    value = (dict(version=3, enabled=True, method='password', verifier=verifier,
                                  auto_lock=config_value['auto_lock']) if kind == 'app' else dict(verifier=verifier))
                    atomic(target, packed(value).encode())
                    # Proof never returns a session. Every old viewing lease
                    # ends, including app locks that use the Hidden password.
                    app_lock.revoke(library)
                    vault.revoke_all(library)
                    access_sessions.clear_attempts(library, scope)
        except LibraryError:
            access_sessions.failed_attempt(library, scope)
            raise
        except (ValueError, UnicodeError, RecursionError):
            access_sessions.failed_attempt(library, scope)
            raise LibraryError('Existing backup identity is invalid; keys were preserved') from None
    return ('App password recovered; unlock Photos with your new password' if kind == 'app' else
            'Hidden password recovered; unlock again with your new password')


def route(library, request, config):
    action = request.get('action')
    if action not in {'recovery_reset_app', 'recovery_reset_hidden'}:
        return None
    # This route is allowed before app authentication, but provides no gallery,
    # tokens, private paths, sync operation or key export.
    try:
        value = json.loads(request.get('value', ''))
        if type(value) is not list or len(value) != 3 or not all(isinstance(v, str) for v in value):
            raise LibraryError('Choose a recovery file, its password and a new local password')
        message = reset(library, config, *value, 'app' if action == 'recovery_reset_app' else 'hidden')
        return dict(app_lock.empty(library, message), recovery_reset=1)
    except LibraryError as error:
        return dict(app_lock.empty(library), recovery_reset=0, error=str(error))
    except (ValueError, TypeError, RecursionError):
        return dict(app_lock.empty(library), recovery_reset=0, error='Choose a recovery file, its password and a new local password')
