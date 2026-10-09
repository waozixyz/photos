"""Private pipe adapter for native paged-sync merges and checkpoints."""
import json
import os
from pathlib import Path
import struct
import subprocess

from .library import packed


def program():
    package = Path(__file__).resolve().parents[1]
    path = next((p for p in [package/'sync_page', package/'build/sync-page/sync_page'] if p.is_file()), None)
    if path is None:
        raise ValueError('Build or install native Photos sync pages before backing up or restoring')
    return path


def apply(library, result, since, cursor, decoded, limit):
    if library.db.in_transaction:
        raise ValueError('Finish the current library edit before syncing; the cursor was retained')
    data = packed(dict(since=since, cursor=cursor, limit=limit, response=result, decoded=decoded)).encode()
    if len(data) > 128*1024*1024:
        raise ValueError('Sync page exceeds its safe limit; changes remain queued')
    environment = {key: value for key, value in os.environ.items() if key not in {
        'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'}}
    try:
        result = subprocess.run(['prlimit', '--core=0', '--as=1073741824', '--cpu=60', '--',
            str(program()), str(library.root/'library.sqlite')], input=struct.pack('!I', len(data))+data,
            env=environment, capture_output=True, timeout=65)
        reply = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise ValueError('The native sync page could not finish; originals and keys were preserved') from None
    if result.returncode or result.stderr or reply != {'saved': 1}:
        raise ValueError(reply.get('error', 'The native sync page could not finish; changes remain queued'))
