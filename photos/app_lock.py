"""Local app access lock using the existing native password verifier."""
import hashlib
import json
import os

from .library import LibraryError, atomic, packed
from . import vault
from . import access_sessions

AUTO_LOCK_SECONDS = {0, 5, 15, 60, 300, 1800}


def path(library):
    return library.root/'private/app-lock.json'


def settings(library):
    marker = path(library)
    if not os.path.lexists(marker):
        return dict(enabled=False, method='hidden', verifier='', auto_lock=0)
    try:
        if marker.is_symlink() or marker.stat().st_size > 1024:
            raise ValueError()
        value = json.loads(marker.read_bytes())
        if not isinstance(value, dict) or type(value.get('version')) is not int or type(value.get('enabled')) is not bool:
            raise ValueError()
        if value['version'] == 1 and set(value) == {'version', 'enabled'}:
            return dict(enabled=value['enabled'], method='hidden', verifier='', auto_lock=0)
        required = {'version', 'enabled', 'method', 'verifier'}
        if value['version'] == 3: required.add('auto_lock')
        if value['version'] not in {2, 3} or set(value) != required:
            raise ValueError()
        if value['method'] not in {'hidden', 'pin', 'password'} or not isinstance(value['verifier'], str):
            raise ValueError()
        verifier = value['verifier']
        if value['method'] == 'hidden':
            if verifier: raise ValueError()
        elif not verifier.startswith('$argon2id$') or len(verifier) >= 128:
            raise ValueError()
        verifier.encode('ascii')
        delay = value.get('auto_lock', 0)
        if type(delay) is not int or delay not in AUTO_LOCK_SECONDS: raise ValueError()
        value['auto_lock'] = delay
        return value
    except (OSError, ValueError, UnicodeError, TypeError, RecursionError):
        # Preserve recovery of old damaged markers using the existing Hidden
        # verifier, while never treating an unreadable marker as disabled.
        return dict(enabled=True, method='hidden', verifier='', auto_lock=0)


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
    access_sessions.revoke(library, access_sessions.APP)


def unlocked(library, token, renew=False):
    stamp = fingerprint(library)
    return bool(stamp and access_sessions.deadline(library, access_sessions.APP, token, stamp, renew) > 0)


def grant(library):
    stamp = fingerprint(library)
    if not stamp:
        raise LibraryError('Local password settings need recovery; originals and backup keys remain intact')
    return access_sessions.grant(library, access_sessions.APP, stamp)


def state(library, token='', renew=False):
    config = settings(library); active = config['enabled']
    return dict(app_lock_enabled=int(active), app_locked=int(active and not unlocked(library, token, renew)),
                app_lock_configured=int(vault.configured(library)),
                app_lock_delay=config['auto_lock'],
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
    access_sessions.check_attempts(library, access_sessions.APP)
    try:
        result = vault.verify_password(password, config['verifier'], pin=config['method'] == 'pin')
    except LibraryError:
        access_sessions.failed_attempt(library, access_sessions.APP)
        raise
    if result.returncode:
        access_sessions.failed_attempt(library, access_sessions.APP)
        raise LibraryError('Incorrect PIN' if config['method'] == 'pin' and result.returncode == 1 else
                           ('Incorrect password' if result.returncode == 1 else 'App authentication could not finish'))
    access_sessions.clear_attempts(library, access_sessions.APP)


def lock_hidden(library):
    vault.revoke_all(library)


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
    if action == 'app_lock_delay':
        try:
            value = request.get('value')
            if not active: raise LibraryError('Enable App lock before choosing its background delay')
            if not isinstance(value, str) or value not in {str(i) for i in AUTO_LOCK_SECONDS}:
                raise LibraryError('Choose Immediately, 5 seconds, 15 seconds, 1 minute, 5 minutes or 30 minutes')
            config = {**settings(library), 'version': 3, 'auto_lock': int(value)}
            atomic(path(library), packed(config).encode())
            revoke(library); token = grant(library)
            return dict(state(library, token), app_lock_token=token, app_lock_checked=1,
                        status='Background lock delay saved on this device')
        except LibraryError as error:
            return dict(state(library, token), error=str(error), app_lock_checked=1)
    if action in {'app_lock_enable', 'app_lock_disable', 'app_lock_pin', 'app_lock_password'}:
        try:
            if action in {'app_lock_pin', 'app_lock_password'}:
                method = 'pin' if action == 'app_lock_pin' else 'password'
                verifier = vault.hash_password(request.get('value'), pin=method == 'pin')
                config = dict(version=3, enabled=True, method=method, verifier=verifier,
                              auto_lock=settings(library)['auto_lock'])
            else:
                if action == 'app_lock_enable':
                    hidden_token = vault.authenticate(library, request.get('value'))
                    vault.lock(library, hidden_token)
                else:
                    authenticate(library, request.get('value'))
                config = (dict(version=3, enabled=True, method='hidden', verifier='',
                               auto_lock=settings(library)['auto_lock']) if action == 'app_lock_enable'
                          else dict(version=1, enabled=False))
            if active and not unlocked(library, token):
                raise LibraryError('App access ended; unlock before changing its settings')
            atomic(path(library), packed(config).encode())
            revoke(library)
            lock_hidden(library); access_sessions.clear_attempts(library, access_sessions.APP)
            return dict(empty(library, 'App lock disabled' if action == 'app_lock_disable' else 'App lock enabled'),
                        app_lock_changed=1)
        except LibraryError as error:
            return dict(empty(library) if active else state(library, token), error=str(error))
    if action == 'app_lock_check':
        return dict(state(library, token, renew=True), app_lock_checked=1)
    return None
