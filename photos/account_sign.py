"""Private byte adapter for native post-quantum account keys and signatures."""
import os
from pathlib import Path
import struct
import subprocess

PUBLIC_BYTES = 1312
PRIVATE_BYTES = 2560
SIGNATURE_BYTES = 2420
LIMIT = 1024*1024


def program():
    root = Path(__file__).resolve().parents[1]
    candidates = [root/'account_sign', root/'build/account-sign/account_sign']
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        raise ValueError('Build or install native Photos account signing before backing up or restoring')
    return path


def request(mode, message=b'', private=b'', public=b'', signature=b''):
    if (type(mode) is not int or mode not in {1,2,3} or
            any(type(value) is not bytes for value in [message,private,public,signature]) or len(message) > LIMIT):
        raise ValueError('Invalid Photos account signing request')
    if ((mode == 1 and (message or private or public or signature)) or
            (mode == 2 and (len(private) != PRIVATE_BYTES or public or signature)) or
            (mode == 3 and (private or len(public) != PUBLIC_BYTES or len(signature) != SIGNATURE_BYTES))):
        raise ValueError('Invalid Photos account key or signature')
    data = struct.pack('!BBI',1,mode,len(message))+private+public+signature+message
    environment = {k: v for k,v in os.environ.items() if k not in
                   {'DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','DBUS_SESSION_BUS_ADDRESS','SESSION_MANAGER'}}
    try:
        result = subprocess.run(['prlimit','--core=0','--as=268435456','--cpu=20','--',str(program())],
                                input=data,env=environment,capture_output=True,timeout=30)
    except (OSError,subprocess.TimeoutExpired):
        raise ValueError('Photos account signing is unavailable; originals and keys were preserved') from None
    if mode == 3 and result.returncode == 1 and not result.stdout and not result.stderr:
        return False
    expected = PUBLIC_BYTES+PRIVATE_BYTES if mode == 1 else SIGNATURE_BYTES if mode == 2 else 0
    if result.returncode != 0 or result.stderr or len(result.stdout) != expected:
        raise ValueError('Photos account signing could not finish; originals and keys were preserved')
    return True if mode == 3 else result.stdout


def generate_keypair():
    value = request(1)
    return value[:PUBLIC_BYTES],value[PUBLIC_BYTES:]


def sign(private, message):
    return request(2,message,private=private)


def verify(public, message, signature):
    return request(3,message,public=public,signature=signature)
