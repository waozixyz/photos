"""Private binary adapter to DaochiClient's signed transaction protocol."""
import hashlib
import os
from pathlib import Path
import struct
import subprocess

METHODS = {"GET": 1, "POST": 2, "PUT": 3, "HEAD": 4}


def program():
    root = Path(__file__).resolve().parents[1]
    path = next((p for p in (root / "sync_transaction", root / "build/sync-transaction/sync_transaction")
                 if p.is_file()), None)
    if path is None:
        raise ValueError("Build or install native Daochi request signing before syncing")
    return path


def header(public, private, seed, method, path, body, expires):
    if (type(public) is not bytes or len(public) != 1312 or type(private) is not bytes or len(private) != 2560
            or type(seed) is not bytes or len(seed) != 32 or method not in METHODS or type(path) is not str
            or type(body) is not bytes or type(expires) is not int or not 0 < expires < 2**63):
        raise ValueError("Invalid Daochi signed request")
    try:
        target = path.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("Invalid Daochi request path") from None
    if not 0 < len(target) <= 1024:
        raise ValueError("Invalid Daochi request path")
    request = (struct.pack("!BBHQ", 1, METHODS[method], len(target), expires)
               + public + private + seed + hashlib.sha256(body).digest() + target)
    environment = {k: v for k, v in os.environ.items() if k not in
                   {"DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "SESSION_MANAGER"}}
    try:
        result = subprocess.run(["prlimit", "--core=0", "--as=268435456", "--cpu=20", "--", str(program())],
                                input=request, env=environment, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("Daochi request signing is unavailable; existing account and device keys were preserved") from None
    if result.returncode or result.stderr or not 0 < len(result.stdout) < 8192:
        raise ValueError("Daochi request signing failed; existing account and device keys were preserved")
    try:
        return result.stdout.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("Invalid Daochi signed response") from None
