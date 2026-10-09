"""Transport to the native in-memory access registry; no Python session store."""
import atexit
import hashlib
import os
from pathlib import Path
import select
import struct
import subprocess
import threading
import time

from .library import LibraryError

HIDDEN = 1
APP = 2


def program():
    package = Path(__file__).resolve().parents[1]
    path = next((p for p in [package/'access_sessions',package/'build/access-sessions/access_sessions'] if p.is_file()),None)
    if path is None:
        raise LibraryError('Install native Photos access sessions before unlocking')
    return path


class Registry:
    def __init__(self, session_ms=600000, attempt_ms=60000):
        if (type(session_ms) is not int or not 1 <= session_ms <= 600000 or
                type(attempt_ms) is not int or not 1 <= attempt_ms <= 60000):
            raise ValueError('Invalid access lifetime')
        self.session_ms = session_ms
        self.attempt_ms = attempt_ms
        self.guard = threading.RLock()
        self.child = None

    def close(self):
        with self.guard:
            child, self.child = self.child, None
            if child is not None:
                child.terminate()
                try: child.wait(timeout=2)
                except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=2)
                child.stdin.close(); child.stdout.close()

    def request(self, op, kind, root, token='', stamp='', renew=False):
        if (type(op) is not int or op not in range(1,9) or kind not in {HIDDEN,APP} or
                not isinstance(root,str) or not root or '\0' in root or len(root.encode()) > 16384 or
                not isinstance(token,str) or (token and (len(token)!=64 or any(c not in '0123456789abcdef' for c in token))) or
                not isinstance(stamp,str) or (stamp and (len(stamp)!=64 or any(c not in '0123456789abcdef' for c in stamp)))):
            raise LibraryError('Invalid local access session')
        frame = bytes([1,op,kind,int(renew)])+hashlib.sha256(root.encode()).digest()+bytes.fromhex(token or '00'*32)+bytes.fromhex(stamp or '00'*32)
        with self.guard:
            try:
                if self.child is not None and self.child.poll() is not None:
                    # Losing the native process invalidates every old token.
                    self.close()
                if self.child is None:
                    environment = {k:v for k,v in os.environ.items() if k not in
                        {'DISPLAY','WAYLAND_DISPLAY','XAUTHORITY','DBUS_SESSION_BUS_ADDRESS','SESSION_MANAGER'}}
                    self.child = subprocess.Popen(['prlimit','--core=0','--as=134217728','--',str(program()),str(self.session_ms),str(self.attempt_ms)],
                        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=environment,bufsize=0)
                end = time.monotonic()+3
                outgoing = memoryview(frame)
                while outgoing:
                    if not select.select([],[self.child.stdin],[],max(0,end-time.monotonic()))[1]: raise OSError()
                    written = os.write(self.child.stdin.fileno(),outgoing)
                    if written <= 0: raise OSError()
                    outgoing = outgoing[written:]
                response = bytearray()
                while len(response) < 41:
                    if not select.select([self.child.stdout],[],[],max(0,end-time.monotonic()))[0]: raise OSError()
                    block = os.read(self.child.stdout.fileno(),41-len(response))
                    if not block: raise OSError()
                    response.extend(block)
                code = response[0]; until = struct.unpack('!Q',response[33:])[0]/1000
                if code not in {0,1,2,3} or (op!=1 and any(response[1:33])):
                    raise OSError()
                if code == 2: raise LibraryError('Local access session capacity reached; wait or restart Photos')
                if code == 3: raise LibraryError('Too many attempts; wait one minute before trying again')
                return code == 1, response[1:33].hex(), until
            except LibraryError:
                raise
            except (OSError,ValueError,subprocess.SubprocessError):
                self.close()
                raise LibraryError('Photos access sessions are unavailable; unlock again when the service returns') from None


registry = Registry()
atexit.register(registry.close)


def grant(library, kind, stamp=''):
    ok, token, _ = registry.request(1,kind,str(library.root),stamp=stamp)
    if not ok or token == '00'*32:
        raise LibraryError('Photos could not grant local access')
    return token


def deadline(library, kind, token, stamp='', renew=False):
    if not isinstance(token,str) or len(token)!=64:
        return 0
    try:
        ok, _, until = registry.request(2,kind,str(library.root),token,stamp,renew)
        return until if ok else 0
    except LibraryError:
        return 0


def lock(library, kind, token):
    if not isinstance(token,str) or len(token)!=64 or any(c not in '0123456789abcdef' for c in token):
        return
    try: registry.request(3,kind,str(library.root),token)
    except LibraryError: registry.close()


def revoke(library, kind):
    try: registry.request(4,kind,str(library.root))
    except LibraryError: registry.close()


def check_attempts(library, kind):
    registry.request(6,kind,str(library.root))


def failed_attempt(library, kind):
    registry.request(7,kind,str(library.root))


def clear_attempts(library, kind):
    registry.request(8,kind,str(library.root))
