"""Binary adapter for the native client encryption engine; no Python fallback."""
import os
from pathlib import Path
import struct
import subprocess

LIMIT = 128*1024*1024


def program():
    root = Path(__file__).resolve().parents[1]
    candidates = [root/'sync_crypto', root/'build/sync-crypto/sync_crypto']
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        raise ValueError('Build or install native Photos encryption before backing up or restoring')
    return path


def transform(mode, content, aad, nonce, key):
    if (mode not in range(1,8) or type(content) is not bytes or type(aad) is not bytes or type(nonce) is not bytes or
            type(key) is not bytes or len(key) != 32 or len(nonce) != 24 or len(aad) > 1024*1024 or
            len(content) > LIMIT+(16 if mode == 2 else 0) or (mode == 2 and len(content) < 16)):
        raise ValueError('Invalid encrypted Photos payload')
    if mode >= 5 and (aad or nonce != b'\0'*24 or (mode >= 6 and content) or (mode == 7 and key != b'\0'*32)):
        raise ValueError('Invalid Photos device signature request')
    request = struct.pack('!BBII', 1, mode, len(content), len(aad))+key+nonce+aad+content
    environment = {k: v for k, v in os.environ.items() if k not in
                   {'DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','DBUS_SESSION_BUS_ADDRESS','SESSION_MANAGER'}}
    try:
        result = subprocess.run(['prlimit','--core=0','--as=536870912','--cpu=20','--',str(program())],
                                input=request, env=environment, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('Photos encryption is unavailable; original files and keys were preserved') from None
    expected = 64 if mode == 5 else 32 if mode in {6,7} else len(content)-16 if mode == 2 else len(content)+(40 if mode in {3,4} else 16)
    if result.returncode != 0 or len(result.stdout) != expected or result.stderr:
        raise ValueError('Encrypted Photos payload failed authentication' if mode == 2 and result.returncode == 1
                         else 'Photos encryption could not finish; original files and keys were preserved')
    return result.stdout


def encrypt(content, aad, nonce, key):
    return transform(1, content, aad, nonce, key)


def decrypt(content, aad, nonce, key):
    return transform(2, content, aad, nonce, key)


def seal_blob(content, aad, key):
    return transform(3, content, aad, b'\0'*24, key)


def seal_record(content, aad, key):
    return transform(4, content, aad, b'\0'*24, key)


def device_signature(content, seed):
    return transform(5, content, b'', b'\0'*24, seed)


def device_public(seed):
    return transform(6, b'', b'', b'\0'*24, seed)


def device_seed():
    return transform(7, b'', b'', b'\0'*24, b'\0'*32)
