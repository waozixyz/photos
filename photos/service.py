"""Private, bounded local RPC shared by the standalone app and signed bundle."""
import fcntl
import json
import os
from pathlib import Path
import socket
import socketserver
import struct

from .library import packed
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


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(60)
        try:
            _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                return
            length = struct.unpack('!I', receive(self.request, 4))[0]
            if not 0 < length <= LIMIT:
                return
            value = json.loads(receive(self.request, length))
            if not isinstance(value, dict):
                return
            result = process(value, self.server.config)
        except Exception as error:
            result = dict(error=f'Local request failed ({type(error).__name__}); your originals are safe')
        output = packed(result).encode()
        if len(output) > LIMIT:
            output = packed(dict(error='This page exceeds the preview limit')).encode()
        try:
            self.request.sendall(struct.pack('!I', len(output)) + output)
        except OSError:
            pass


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, config, path=None):
        self.config = config
        self.path = Path(path or socket_path())
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self.lock = self.path.with_suffix('.lock').open('a')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # The lock proves no other installed Photos server owns this endpoint.
        self.path.unlink(missing_ok=True)
        super().__init__(str(self.path), Handler)
        os.chmod(self.path, 0o600)

    def server_close(self):
        super().server_close()
        self.path.unlink(missing_ok=True)
        self.lock.close()


def main():
    with Server(configuration()) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
