"""Local app access lock using the existing native password verifier."""
import hashlib
import json
import os
import secrets
import time

from .library import LibraryError, atomic, packed
from . import vault

sessions = {}
SESSION_SECONDS = 600


def path(library):
    return library.root/'private/app-lock.json'


def enabled(library):
    marker = path(library)
    if not os.path.lexists(marker):
        return False
    try:
        if marker.is_symlink() or marker.stat().st_size > 1024:
            return True
        value = json.loads(marker.read_bytes())
        return not (isinstance(value, dict) and set(value) == {'version', 'enabled'} and
                    type(value['version']) is int and value['version'] == 1 and value['enabled'] is False)
    except (OSError, ValueError, UnicodeError):
        return True


def fingerprint(library):
    marker = library.root/'private/hidden-password.json'
    try:
        if marker.is_symlink() or marker.stat().st_size > 1024:
            return ''
        return hashlib.sha256(marker.read_bytes()).hexdigest()
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
    active = enabled(library)
    return dict(app_lock_enabled=int(active), app_locked=int(active and not unlocked(library, token, renew)),
                app_lock_configured=int(vault.configured(library)))


def empty(library, message='Photos is locked'):
    return dict(state(library), photos=[], albums=[], count=0, total=0, pending=0,
                vault_unlocked=0, vault_token='', status=message)


def authenticate(library, password):
    # App authentication never grants or returns Hidden access.
    token = vault.authenticate(library, password)
    vault.lock(library, token)


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
        with vault.guard:
            for existing, entry in list(vault.sessions.items()):
                if entry[0] == str(library.root):
                    vault.sessions.pop(existing, None)
        return empty(library)
    if active and not unlocked(library, token):
        return empty(library)
    if action in {'app_lock_enable', 'app_lock_disable'}:
        try:
            authenticate(library, request.get('value'))
            atomic(path(library), packed(dict(version=1, enabled=action == 'app_lock_enable')).encode())
            revoke(library)
            with vault.guard:
                for existing, entry in list(vault.sessions.items()):
                    if entry[0] == str(library.root):
                        vault.sessions.pop(existing, None)
            return dict(empty(library, 'App lock enabled' if action == 'app_lock_enable' else 'App lock disabled'),
                        app_lock_changed=1)
        except LibraryError as error:
            return dict(state(library, token), error=str(error))
    if action == 'app_lock_check':
        return dict(state(library, token, renew=True), app_lock_checked=1)
    return None
