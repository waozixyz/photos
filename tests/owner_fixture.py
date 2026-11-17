"""A fixture Mesh account, so tests never read the owner's Inbe keys."""
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from photos import account_sign, owner_account

ACCOUNT = owner_account.Account(*account_sign.generate_keypair())
# Another person's account, for libraries that must not share photos.
OTHER = owner_account.Account(*account_sign.generate_keypair())


def database(path, account=ACCOUNT, user='fixture'):
    """An Inbe settings table holding the account, as desktop Inbe keeps it."""
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS settings(user_id TEXT,key TEXT,value TEXT,updated_at INTEGER)')
        db.executemany('INSERT INTO settings VALUES(?,?,?,0)', [
            (user, 'sync_public_id', account.account), (user, 'sync_public_key', account.public.hex()),
            (user, 'sync_private_key', account.private.hex())])
    return path
