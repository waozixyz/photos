"""Library adapter behind the native, same-user Ziran local endpoint."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import threading

from .library import Library, LibraryError, packed
from .worker import configuration, process

LIMIT = 1024 * 1024


def socket_path():
    root = os.environ.get('KRYON_SERVICE_ROOT') or os.environ.get('XDG_RUNTIME_DIR')
    if not root:
        raise ValueError('A private user runtime directory is required')
    path = Path(root) / 'kryon/services/photos.sock'
    if not Path(root).is_absolute() or len(os.fsencode(path)) >= 108:
        raise ValueError('Invalid user runtime directory')
    return path


def receive(connection, count):
    result = bytearray()
    while len(result) < count:
        block = connection.recv(count - len(result))
        if not block:
            raise ValueError('Incomplete local request')
        result.extend(block)
    return bytes(result)


def program():
    root = Path(__file__).resolve().parents[1]
    candidates = [root/'photos_service', root/'build/photos-service/photos_service']
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        raise RuntimeError('Build or install the native Photos service before opening Photos')
    return path


class Server:
    """Preserve the test/launcher lifecycle while Ziran owns all client I/O."""

    def __init__(self, config, path=None):
        self.config = config
        self.path = Path(path or socket_path())
        if not self.path.is_absolute() or len(os.fsencode(self.path)) >= 108:
            raise ValueError('Invalid local Photos endpoint')
        # Never follow a substituted runtime-directory or lock-file link.
        if any(p.is_symlink() for p in [self.path.parent, *self.path.parent.parents]):
            raise ValueError('Local Photos endpoint refuses directory links')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.stat().st_uid != os.getuid():
            raise ValueError('Local Photos endpoint must belong to this user')
        os.chmod(self.path.parent, 0o700)
        descriptor = os.open(self.path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
        self.lock = os.fdopen(descriptor, 'rb+')
        self.channel = None
        self.child = None
        self.inode = None
        self.closed = False
        self.stopping = threading.Event()
        self.finished = threading.Event()
        self.serving = False
        self.write_guard = threading.Lock()
        self.capacity = threading.BoundedSemaphore(16)
        self.jobs = ThreadPoolExecutor(max_workers=16, thread_name_prefix='photos-library')
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ValueError('Invalid local Photos endpoint lock')
            os.fchmod(descriptor, 0o600)
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.path.lexists(self.path):
                info = self.path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise ValueError('Existing local Photos endpoint was preserved')
                self.path.unlink()
            self.channel, child_channel = socket.socketpair()
            self.channel.settimeout(5)
            environment = {k: v for k, v in os.environ.items() if k not in
                           {'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'}}
            try:
                self.child = subprocess.Popen(['prlimit', '--core=0', '--', str(program()), str(self.path)], stdin=child_channel,
                                              stdout=child_channel, env=environment, close_fds=True)
            finally:
                child_channel.close()
            if receive(self.channel, 8) != b'\0'*8:
                raise RuntimeError('Native Photos endpoint did not become ready')
            self.channel.settimeout(None)
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise RuntimeError('Native Photos endpoint permissions are invalid')
            self.inode = (info.st_dev, info.st_ino)
        except Exception:
            self.server_close()
            raise

    def dispatch(self, identity, request):
        try:
            self.handle(identity, request)
        finally:
            self.capacity.release()

    def handle(self, identity, request):
        try:
            value = json.loads(request)
            if not isinstance(value, dict):
                raise ValueError('Invalid library request')
            result = process(value, self.config)
        except LibraryError as error:
            result = dict(error=str(error))
            from . import vault
            library = Library(self.config['root'])
            try:
                result.update(vault.state(library, value.get('vault_token', '')))
            finally:
                library.db.close()
        except Exception as error:
            result = dict(error=f'Local request failed ({type(error).__name__}); your originals are safe')
        try:
            output = packed(result).encode()
            if not 0 < len(output) <= LIMIT:
                output = packed(dict(error='This page exceeds the preview limit')).encode()
            with self.write_guard:
                if not self.stopping.is_set():
                    self.channel.sendall(struct.pack('!II', identity, len(output)) + output)
        except (OSError, ValueError, TypeError):
            # A disconnected bridge never receives late results or credentials.
            pass

    def serve_forever(self, poll_interval=0.5):
        del poll_interval
        self.serving = True
        try:
            while not self.stopping.is_set():
                identity, length = struct.unpack('!II', receive(self.channel, 8))
                if identity == 0 or not 0 < length <= LIMIT:
                    raise ValueError('Invalid native Photos request frame')
                request = receive(self.channel, length)
                while not self.stopping.is_set():
                    if self.capacity.acquire(timeout=0.2):
                        try:
                            self.jobs.submit(self.dispatch, identity, request)
                        except RuntimeError:
                            self.capacity.release()
                            if not self.stopping.is_set(): raise
                        break
        except (OSError, ValueError):
            if not self.stopping.is_set():
                raise RuntimeError('Native Photos endpoint stopped; restart Photos to reconnect') from None
        finally:
            self.finished.set()

    def shutdown(self):
        self.stopping.set()
        if self.channel is not None:
            try: self.channel.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        if self.serving:
            self.finished.wait(5)

    def server_close(self):
        if self.closed:
            return
        self.closed = True
        self.shutdown()
        self.jobs.shutdown(wait=False, cancel_futures=True)
        if self.channel is not None:
            self.channel.close()
        if self.child is not None:
            try: self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.child.terminate()
                try: self.child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.child.kill(); self.child.wait(timeout=5)
        if self.inode is not None:
            try:
                info = self.path.lstat()
                if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == self.inode:
                    self.path.unlink()
            except FileNotFoundError:
                pass
        self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.server_close()


def main():
    with Server(configuration()) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
