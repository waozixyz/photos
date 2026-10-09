"""Actual native endpoint: hostile frames, concurrent clients and ownership."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from photos import service


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root/'kryon/services/photos.sock'
        self.calls = []
        def process(request, config):
            self.calls.append(request)
            return dict(echo=request, owner=config['root'])
        self.patch = patch.object(service, 'process', process)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.server = service.Server(dict(root=str(self.root/'library')), self.path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown(); self.server.server_close()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

    def connect(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5); client.connect(str(self.path))
        self.addCleanup(client.close)
        return client

    def request(self, value, fragmented=False):
        data = json.dumps(value, ensure_ascii=False).encode()
        client = self.connect(); frame = struct.pack('!I', len(data))+data
        if fragmented:
            for byte in frame[:12]:
                client.sendall(bytes([byte])); time.sleep(.002)
            for at in range(12, len(frame), 11): client.sendall(frame[at:at+11])
        else: client.sendall(frame)
        length, = struct.unpack('!I', service.receive(client, 4))
        self.assertLessEqual(length, service.LIMIT)
        value = json.loads(service.receive(client, length))
        client.close()
        return value

    def test_fragmented_utf8_and_large_request_preserve_exact_framing(self):
        for value in [dict(action='query', text='家族 📷 café'), dict(text='x'*300000)]:
            result = self.request(value, fragmented=len(value.get('text','')) < 100)
            self.assertEqual(result['echo'], value)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertIsNone(self.server.child.poll())
        limits = Path('/proc', str(self.server.child.pid), 'limits').read_text()
        core = next(line for line in limits.splitlines() if line.startswith('Max core file size'))
        self.assertEqual(core.split()[4:6], ['0', '0'])

    def test_invalid_lengths_json_depth_utf8_and_truncated_clients_never_dispatch(self):
        bodies = [b'[]', b'null', b'{"x":NaN}', b'{} trailing', b'{"x":"\xff"}',
                  b'{"x":'+b'['*70+b'0'+b']'*70+b'}', b'{"x":1,}']
        frames = [struct.pack('!I', 0), struct.pack('!I', service.LIMIT+1)]
        frames += [struct.pack('!I',len(body))+body for body in bodies]
        for frame in frames:
            client = self.connect(); client.sendall(frame)
            try: self.assertEqual(client.recv(1), b'')
            except ConnectionResetError: pass
            client.close()
        client = self.connect(); client.sendall(b'\0\0'); client.close()
        self.assertFalse(self.calls)
        self.assertEqual(self.request(dict(action='query'))['echo']['action'], 'query')

    def test_partial_client_cannot_hold_other_requests_and_connections_are_bounded(self):
        partial = self.connect(); partial.sendall(b'\0')
        self.assertEqual(self.request(dict(action='query'))['echo']['action'], 'query')
        partial.close()
        held = [self.connect() for _ in range(16)]
        # Wait until the native broker accepted every established connection.
        time.sleep(.2)
        extra = self.connect(); extra.sendall(b'\0')
        try: self.assertEqual(extra.recv(1), b'')
        except ConnectionResetError: pass
        extra.close()
        for client in held: client.close()
        time.sleep(.2)
        self.assertEqual(self.request(dict(action='query'))['echo']['action'], 'query')

    def test_concurrent_reordered_responses_and_abandoned_request_keep_identities(self):
        release = threading.Event(); entered = threading.Event()
        def process(request, config):
            if request.get('text') == 'slow':
                entered.set(); release.wait(5)
            else: time.sleep((7-request.get('index',7))*.005)
            return dict(echo=request)
        with patch.object(service, 'process', process):
            client = self.connect(); body = b'{"text":"slow"}'
            client.sendall(struct.pack('!I',len(body))+body)
            self.assertTrue(entered.wait(5)); client.close()
            try:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    values = list(pool.map(lambda index:self.request(dict(index=index)),range(8)))
                self.assertEqual([v['echo']['index'] for v in values], list(range(8)))
                release.set(); time.sleep(.1)
                self.assertEqual(self.request(dict(text='replacement'))['echo']['text'], 'replacement')
            finally: release.set()

    def test_oversized_response_returns_bounded_error_and_keeps_service(self):
        with patch.object(service, 'process', lambda *_:dict(text='x'*(service.LIMIT+1))):
            self.assertIn('preview limit', self.request(dict(action='query'))['error'])
        self.assertEqual(self.request(dict(action='query'))['echo']['action'], 'query')

    def test_second_server_cannot_remove_live_endpoint_and_cleanup_preserves_replacement(self):
        inode = self.path.stat().st_ino
        with self.assertRaises(BlockingIOError):
            service.Server(dict(root='unused'), self.path)
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assertEqual(self.request(dict(action='query'))['echo']['action'], 'query')
        self.path.unlink(); self.path.write_text('replacement belongs to another operation')
        self.server.shutdown(); self.server.server_close()
        self.assertEqual(self.path.read_text(), 'replacement belongs to another operation')
        self.assertIsNotNone(self.server.child.poll())

    def test_stale_endpoint_regular_file_directory_link_and_lock_link_are_preserved(self):
        base = self.root/'other'; base.mkdir()
        endpoint = base/'photos.sock'; endpoint.write_bytes(b'keep')
        with self.assertRaises(ValueError): service.Server(dict(root='unused'), endpoint)
        self.assertEqual(endpoint.read_bytes(), b'keep')
        endpoint.unlink(); lock = endpoint.with_suffix('.lock'); lock.unlink()
        target = base/'keep'; target.write_bytes(b'private')
        lock.symlink_to(target)
        with self.assertRaises(OSError): service.Server(dict(root='unused'), endpoint)
        self.assertEqual(target.read_bytes(), b'private')
        linked = self.root/'linked'; linked.symlink_to(base, target_is_directory=True)
        with self.assertRaises(ValueError): service.Server(dict(root='unused'), linked/'photos.sock')
        lock.unlink()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(endpoint)); stale.close()
        with service.Server(dict(root='unused'), endpoint) as replacement:
            self.assertTrue(stat.S_ISSOCK(endpoint.stat().st_mode))
            self.assertIsNone(replacement.child.poll())
        self.assertFalse(endpoint.exists())

    def test_native_receive_deadline_reclaims_partial_connection_without_stopping_queries(self):
        client = self.connect(); client.settimeout(65); client.sendall(b'\0')
        started = time.monotonic()
        self.assertEqual(self.request(dict(action='query'))['echo']['action'], 'query')
        self.assertEqual(client.recv(1), b'')
        self.assertGreaterEqual(time.monotonic()-started, 59)
        self.assertLess(time.monotonic()-started, 64)
        self.assertEqual(self.request(dict(action='query'))['echo']['action'], 'query')


if __name__ == '__main__': unittest.main()
