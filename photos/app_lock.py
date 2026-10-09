"""Local app access lock using the existing native password verifier."""
import hashlib
import json
import os
import secrets
import time

from .library import LibraryError, atomic, packed
from . import vault

sessions = {}
failures = {}
SESSION_SECONDS = 600


def path(library):
    return library.root/'private/app-lock.json'


def settings(library):
    marker = path(library)
    if not os.path.lexists(marker):
        return dict(enabled=False, method='hidden', verifier='')
    try:
        if marker.is_symlink() or marker.stat().st_size > 1024:
            raise ValueError()
        value = json.loads(marker.read_bytes())
        if not isinstance(value, dict) or type(value.get('version')) is not int or type(value.get('enabled')) is not bool:
            raise ValueError()
        if value['version'] == 1 and set(value) == {'version', 'enabled'}:
            return dict(enabled=value['enabled'], method='hidden', verifier='')
        if value['version'] != 2 or set(value) != {'version', 'enabled', 'method', 'verifier'}:
            raise ValueError()
        if value['method'] not in {'hidden', 'pin', 'password'} or not isinstance(value['verifier'], str):
            raise ValueError()
        verifier = value['verifier']
        if value['method'] == 'hidden':
            if verifier: raise ValueError()
        elif not verifier.startswith('$argon2id$') or len(verifier) >= 128:
            raise ValueError()
        verifier.encode('ascii')
        return value
    except (OSError, ValueError, UnicodeError, TypeError, RecursionError):
        # Preserve recovery of old damaged markers using the existing Hidden
        # verifier, while never treating an unreadable marker as disabled.
        return dict(enabled=True, method='hidden', verifier='')


def enabled(library):
    return settings(library)['enabled']


def fingerprint(library):
    config = settings(library)
    marker = library.root/'private/hidden-password.json' if config['method'] == 'hidden' else path(library)
    try:
        if marker.is_symlink() or marker.stat().st_size > 1024:
            return ''
        return hashlib.sha256(packed(config).encode()+marker.read_bytes()).hexdigest()
    except OSError:
        return ''


def revoke(library):
    for token, entry in list(sessions.items()):
        if entry[0] == str(library.root):
            sessions.pop(token, None)


def unlocked(library, token, renew=False):
    entry = sessions.get(token) if isinstance(token, str) else None
    stamp = fingerprint(library)
    if entry and stamp and entry[0] == str(library.root) and entry[1] > time.monotonic() and entry[2] == stamp:
        if renew:
            sessions[token] = (entry[0], time.monotonic()+SESSION_SECONDS, stamp)
        return True
    if entry:
        sessions.pop(token, None)
    return False


def grant(library):
    stamp = fingerprint(library)
    if not stamp:
        raise LibraryError('Local password settings need recovery; originals and backup keys remain intact')
    token = secrets.token_hex(32)
    for existing, entry in list(sessions.items()):
        if entry[1] <= time.monotonic():
            sessions.pop(existing, None)
    sessions[token] = (str(library.root), time.monotonic()+SESSION_SECONDS, stamp)
    return token


def state(library, token='', renew=False):
    config = settings(library); active = config['enabled']
    return dict(app_lock_enabled=int(active), app_locked=int(active and not unlocked(library, token, renew)),
                app_lock_configured=int(vault.configured(library)),
                app_lock_method={'hidden': 0, 'pin': 1, 'password': 2}[config['method']])


def empty(library, message='Photos is locked'):
    return dict(state(library), photos=[], albums=[], count=0, total=0, pending=0,
                vault_unlocked=0, vault_token='', status=message)


def authenticate(library, password):
    # App authentication never grants or returns Hidden access.
    config = settings(library)
    if config['method'] == 'hidden':
        token = vault.authenticate(library, password)
        vault.lock(library, token)
        return
    key = str(library.root); now = time.monotonic()
    attempt = failures.get(key, (now, 0))
    if now - attempt[0] >= 60: attempt = (now, 0)
    if attempt[1] >= 5:
        raise LibraryError('Too many attempts; wait one minute before trying again')
    try:
        result = vault.verify_password(password, config['verifier'], pin=config['method'] == 'pin')
    except LibraryError:
        failures[key] = (attempt[0], attempt[1]+1)
        raise
    if result.returncode:
        failures[key] = (attempt[0], attempt[1]+1)
        raise LibraryError('Incorrect PIN' if config['method'] == 'pin' and result.returncode == 1 else
                           ('Incorrect password' if result.returncode == 1 else 'App authentication could not finish'))
    failures.pop(key, None)


def lock_hidden(library):
    with vault.guard:
        for existing, entry in list(vault.sessions.items()):
            if entry[0] == str(library.root): vault.sessions.pop(existing, None)


def route(library, request):
    action = request.get('action', 'query')
    token = request.get('app_lock_token', '')
    active = enabled(library)
    if action == 'app_lock_unlock':
        try:
            if not active:
                raise LibraryError('App lock is not enabled')
            authenticate(library, request.get('value'))
            token = grant(library)
            return dict(state(library, token), app_lock_token=token, app_lock_authorized=1)
        except LibraryError as error:
            return dict(empty(library), error=str(error))
    if action == 'app_lock_lock':
        # A private same-user client may always tighten access. This also
        # revokes the server session after the UI has already wiped its token.
        revoke(library)
        lock_hidden(library)
        return empty(library)
    if active and not unlocked(library, token):
        return empty(library)
    if action in {'app_lock_enable', 'app_lock_disable', 'app_lock_pin', 'app_lock_password'}:
        try:
            if action in {'app_lock_pin', 'app_lock_password'}:
                method = 'pin' if action == 'app_lock_pin' else 'password'
                verifier = vault.hash_password(request.get('value'), pin=method == 'pin')
                config = dict(version=2, enabled=True, method=method, verifier=verifier)
            else:
                if action == 'app_lock_enable':
                    hidden_token = vault.authenticate(library, request.get('value'))
                    vault.lock(library, hidden_token)
                else:
                    authenticate(library, request.get('value'))
                config = dict(version=1, enabled=action == 'app_lock_enable')
            if active and not unlocked(library, token):
                raise LibraryError('App access ended; unlock before changing its settings')
            atomic(path(library), packed(config).encode())
            revoke(library)
            lock_hidden(library); failures.pop(str(library.root), None)
            return dict(empty(library, 'App lock disabled' if action == 'app_lock_disable' else 'App lock enabled'),
                        app_lock_changed=1)
        except LibraryError as error:
            return dict(empty(library) if active else state(library, token), error=str(error))
    if action == 'app_lock_check':
        return dict(state(library, token, renew=True), app_lock_checked=1)
    return None
