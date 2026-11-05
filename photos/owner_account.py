"""The owner's one Daochi account, read from desktop Inbe; keys are never printed."""
import hashlib
from pathlib import Path
import re
import sqlite3

DATABASE = Path.home() / '.local/share/inbe/inbe.db'
KEYS = ('sync_public_id', 'sync_public_key', 'sync_private_key')
HEX = re.compile(r'^[0-9a-f]+$')


class Account:
    def __init__(self, public, private):
        if type(public) is not bytes or len(public) != 1312 or type(private) is not bytes or len(private) != 2560:
            raise ValueError("Inbe's Daochi account is incomplete; sign in to Inbe on this computer first")
        self.public = public
        self.private = private
        self.account = hashlib.sha256(public).hexdigest()


def load(path=None):
    """The account Inbe signed in with on this desktop, the same one every app uses."""
    path = Path(path or DATABASE)
    try:
        database = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            rows = database.execute('SELECT user_id,key,value FROM settings WHERE key IN (?,?,?)', KEYS).fetchall()
        finally:
            database.close()
    except sqlite3.Error:
        raise ValueError("Inbe's Daochi account is unavailable; sign in to Inbe on this computer first") from None
    users = {}
    for user, key, value in rows:
        users.setdefault(user, {})[key] = value
    complete = [values for values in users.values() if all(values.get(key) for key in KEYS)]
    if len(complete) != 1:
        raise ValueError("Inbe's Daochi account is unavailable; sign in to Inbe on this computer first")
    values = complete[0]
    if not all(isinstance(values[key], str) and HEX.fullmatch(values[key]) for key in KEYS):
        raise ValueError("Inbe's Daochi account is incomplete; sign in to Inbe on this computer first")
    account = Account(bytes.fromhex(values['sync_public_key']), bytes.fromhex(values['sync_private_key']))
    if account.account != values['sync_public_id']:
        raise ValueError("Inbe's Daochi account keys do not match; Photos keys were preserved")
    return account
