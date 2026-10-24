"""Binary adapter for the account's shared entity data; no Python fallback."""
import hashlib
import os
from pathlib import Path
import re
import struct
import subprocess

CHUNK = 8 * 1024 * 1024
KIND = re.compile(r'^[a-z0-9][a-z0-9-]{0,47}$')
HASH = re.compile(r'^[0-9a-f]{64}$')


def program():
    root = Path(__file__).resolve().parents[1]
    path = next((p for p in (root / 'entity_crypto', root / 'build/entity-crypto/entity_crypto') if p.is_file()), None)
    if path is None:
        raise ValueError('Build or install native Photos entity encryption before syncing')
    return path


def transform(mode, private, kind, digest=b'\0' * 64, index=0, content=b''):
    if (mode not in (1, 2, 3) or type(private) is not bytes or not 0 < len(private) <= 8192
            or not isinstance(kind, str) or not KIND.fullmatch(kind) or type(content) is not bytes
            or type(index) is not int or not 0 <= index < 1000000 or len(content) > CHUNK + 16
            or (mode == 1 and content) or (mode != 1 and not content) or (mode == 3 and len(content) <= 16)):
        raise ValueError('Invalid entity encryption request')
    name = kind.encode()
    request = (struct.pack('!BBHIQ', 1, mode, len(name), len(private), index) + digest
               + struct.pack('!I', len(content)) + b'\0' * 4 + private + name + content)
    environment = {k: v for k, v in os.environ.items() if k not in
                   {'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'}}
    try:
        result = subprocess.run(['prlimit', '--core=0', '--as=536870912', '--cpu=20', '--', str(program())],
                                input=request, env=environment, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError('Photos entity encryption is unavailable; originals and keys were preserved') from None
    expected = 32 if mode == 1 else len(content) + 16 if mode == 2 else len(content) - 16
    if result.returncode != 0 or len(result.stdout) != expected or result.stderr:
        raise ValueError('Shared photo content failed authentication' if mode == 3 and result.returncode == 1
                         else 'Photos entity encryption could not finish; originals and keys were preserved')
    return result.stdout


def record_key(private, kind):
    return transform(1, private, kind)


def content_hash(digest):
    if not isinstance(digest, str) or not HASH.fullmatch(digest):
        raise ValueError('Invalid shared content hash')
    return digest.encode()


def seal_chunk(private, kind, digest, index, plain):
    return transform(2, private, kind, content_hash(digest), index, plain)


def open_chunk(private, kind, digest, index, sealed):
    return transform(3, private, kind, content_hash(digest), index, sealed)


def chunks(data):
    """Plain chunks of content: 8 MiB each, only the last shorter."""
    return [data[at:at + CHUNK] for at in range(0, len(data), CHUNK)] or [b'']


def seal(private, kind, data):
    """Sealed chunks of a whole content and its hash, in chunk order."""
    if not data:
        raise ValueError('Shared content must not be empty')
    digest = hashlib.sha256(data).hexdigest()
    return digest, [seal_chunk(private, kind, digest, index, plain) for index, plain in enumerate(chunks(data))]
