"""Compatibility adapter for native immutable-original import."""
import json
import math
import os
from pathlib import Path
import re
import select
import subprocess
import tempfile

from .library import LibraryError, packed


def program():
    root = Path(__file__).resolve().parents[1]
    paths = [root/'import_original', root/'build/import-original/import_original']
    helper = next((path for path in paths if path.is_file()), None)
    if helper is None:
        raise LibraryError('Build or install the Photos importer before importing originals')
    return helper


def metadata_program():
    root = Path(__file__).resolve().parents[1]
    paths = [root/'import_metadata', root/'build/import-metadata/import_metadata']
    helper = next((path for path in paths if path.is_file()), None)
    if helper is None:
        raise LibraryError('Build or install the Photos metadata importer before importing photos')
    return helper


def folder_program():
    root = Path(__file__).resolve().parents[1]
    paths = [root/'import_folders', root/'build/import-folders/import_folders']
    helper = next((path for path in paths if path.is_file()), None)
    if helper is None:
        raise LibraryError('Build or install Photos folder discovery before importing photos')
    return helper


def metadata_reader_program():
    root = Path(__file__).resolve().parents[1]
    paths = [root/'read_metadata', root/'build/read-metadata/read_metadata']
    helper = next((path for path in paths if path.is_file()), None)
    if helper is None:
        raise LibraryError('Build or install Photos metadata reading before importing photos')
    return helper


class Reader:
    """One supervised native reader per import batch, with bounded responses."""
    def __init__(self):
        self.child = None
        self.capture_path = None
        self.capture = {}

    def __enter__(self):
        environment = {key: value for key, value in os.environ.items() if key not in {
            'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'}}
        try:
            self.child = subprocess.Popen(['prlimit', '--as=536870912', '--', str(metadata_reader_program())],
                env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0)
        except OSError:
            raise LibraryError('Metadata reading could not start; retry the import') from None
        return self

    def __exit__(self, *unused):
        if self.child is None:
            return
        child, self.child = self.child, None
        child.stdin.close()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait(timeout=2)
        child.stdout.close()

    def request(self, operation, path, fallback=None):
        if self.child is None:
            raise LibraryError('Metadata reader is not open')
        if self.child.poll() is not None:
            self.__exit__(); self.__enter__()
        request = dict(operation=operation, path=str(Path(path).absolute()))
        if fallback is not None:
            request['fallback'] = fallback
        if operation == 'camera' and self.capture_path == request['path']:
            request.update(self.capture)
        data = packed(request).encode()+b'\n'
        if len(data) > 32768:
            raise LibraryError('Metadata request exceeds its limit')
        try:
            at = 0
            while at < len(data):
                written = self.child.stdin.write(data[at:])
                if not written:
                    raise OSError()
                at += written
            # Unbuffered reads prevent a ready descriptor from hiding bytes in
            # a Python buffer. A stalled codec is cancelled by this request's
            # deadline rather than holding an import indefinitely.
            import time
            deadline = time.monotonic()+20
            result = bytearray()
            while len(result) <= 16*1024*1024:
                left = deadline-time.monotonic()
                if left <= 0 or not select.select([self.child.stdout], [], [], left)[0]:
                    self.child.kill(); self.child.wait(timeout=2)
                    raise LibraryError('Metadata reading timed out; retry the import')
                block = os.read(self.child.stdout.fileno(), 65536)
                if not block:
                    raise LibraryError('Metadata reader stopped; retry the import')
                result.extend(block)
                if len(result) > 16*1024*1024:
                    self.child.kill(); self.child.wait(timeout=2)
                    raise LibraryError('Metadata response exceeds its limit')
                if b'\n' in block:
                    if not result.endswith(b'\n') or result.count(b'\n') != 1:
                        raise LibraryError('Metadata reader returned an invalid response')
                    break
            else:
                self.child.kill(); self.child.wait(timeout=2)
                raise LibraryError('Metadata response exceeds its limit')
            response = json.loads(result)
        except (OSError, ValueError) as error:
            if isinstance(error, LibraryError):
                raise
            raise LibraryError('Metadata reading could not finish; retry the import') from None
        if not isinstance(response, dict):
            raise LibraryError('Metadata reader returned an invalid response')
        if response.get('error'):
            raise LibraryError(response['error'])
        return response


def sidecar(path, reader):
    reader.capture_path = None; reader.capture = {}
    response = reader.request('sidecar', path)
    if not isinstance(response.get('metadata'), dict) or not isinstance(response.get('stamp'), str) or type(response.get('capture_mode')) is not int or response['capture_mode'] not in (0, 1, 2) or type(response.get('capture_value')) not in {int, float} or not math.isfinite(response['capture_value']):
        raise LibraryError('Metadata reader returned an invalid sidecar')
    reader.capture_path = str(Path(path).absolute())
    reader.capture = {key: response[key] for key in ('capture_mode', 'capture_value')}
    return response['metadata'], response['stamp']


def camera(path, fallback, reader):
    response = reader.request('camera', path, fallback)
    gps = response.get('gps')
    if not all(type(response.get(key)) is int and 0 <= response[key] <= 2147483647 for key in ['width', 'height']) or type(response.get('taken')) not in {int, float} or not math.isfinite(response['taken']) or 'gps' not in response or gps is not None and (not isinstance(gps, dict) or any(type(gps.get(key)) not in {int, float} or not math.isfinite(gps[key]) or abs(gps[key]) > limit for key, limit in [('latitude', 90), ('longitude', 180)])):
        raise LibraryError('Metadata reader returned invalid camera fields')
    return response


def album_metadata(path, reader):
    response = reader.request('album', path)
    if not isinstance(response.get('metadata'), dict) or not isinstance(response.get('stamp'), str):
        raise LibraryError('Metadata reader returned invalid album metadata')
    return response['metadata'] if response['stamp'] else None


def walk(root):
    """Bounded streamed entries; closing the consumer stops its own helper."""
    root = Path(root)
    environment = {key: value for key, value in os.environ.items() if key not in {
        'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'}}
    child = None
    try:
        child = subprocess.Popen(['prlimit', '--as=268435456', '--', str(folder_program()), str(root)],
                                 env=environment, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        complete = False
        while True:
            line = child.stdout.readline(65537)
            if not line:
                break
            if len(line) > 65536 or not line.endswith(b'\n'):
                raise LibraryError('Folder discovery returned an oversized entry')
            entry = json.loads(line)
            if not isinstance(entry, dict) or set(entry) != {'kind', 'path'}:
                raise LibraryError('Folder discovery returned an invalid entry')
            kind, name = entry['kind'], entry['path']
            if complete or not isinstance(name, str) or kind not in {'directory', 'file', 'error', 'complete'}:
                raise LibraryError('Folder discovery returned an invalid entry')
            if kind == 'complete':
                if name:
                    raise LibraryError('Folder discovery returned an invalid completion')
                complete = True
                continue
            path = Path(name)
            if not path.is_relative_to(root):
                raise LibraryError('Folder discovery returned a path outside the source')
            yield kind, path
        if child.wait(timeout=5) or not complete:
            raise LibraryError('Folder discovery could not finish; rescan the source')
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        if isinstance(error, LibraryError):
            raise
        raise LibraryError('Folder discovery could not finish; rescan the source') from None
    finally:
        if child is not None:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait(timeout=2)
            child.stdout.close()


def merge(library, request, report=False):
    if library.db.in_transaction:
        raise LibraryError('Finish the current library operation before importing metadata')
    data = packed(request).encode()
    if len(data)+1 > 16*1024*1024:
        raise LibraryError('Import metadata exceeds its limit')
    environment = {key: value for key, value in os.environ.items() if key not in {
        'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'}}
    try:
        result = subprocess.run(['prlimit', '--as=536870912', '--cpu=30', '--',
                                 str(metadata_program()), str(library.root/'library.sqlite')],
                                input=data+b'\n', env=environment, capture_output=True, timeout=40)
        response = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise LibraryError('Import outcome could not be confirmed; rescan before retrying') from None
    if not isinstance(response, dict):
        raise LibraryError('Import returned an invalid response')
    if response.get('error'):
        raise LibraryError(response['error'])
    if result.returncode or response.get('saved') != 1 or response.get('added') not in (0, 1) or response.get('warning', 0) not in (0, 1):
        raise LibraryError('Import outcome could not be confirmed; rescan before retrying')
    return response if report else bool(response['added'])


def copy(source, destination, identity, expected_size=None):
    if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity):
        raise LibraryError('Choose a valid original identity')
    environment = {key: value for key, value in os.environ.items() if key not in {
        'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'}}
    destination = Path(destination)
    if expected_size is None:
        expected_size = Path(source).stat().st_size
    if type(expected_size) is not int or not 0 <= expected_size <= 9223372036854775807:
        raise LibraryError('Choose a valid original byte size')
    fd, name = tempfile.mkstemp(prefix=destination.name+'.import-', dir=destination.parent)
    os.close(fd)
    try:
        result = subprocess.run(['prlimit', '--as=268435456', '--', str(program()),
                                 str(source), str(destination), identity, name, str(expected_size)], env=environment,
                                capture_output=True, timeout=600)
        response = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise LibraryError('Original import could not finish; retry the import') from None
    finally:
        Path(name).unlink(missing_ok=True)
    if not isinstance(response, dict) or response.get('stored') != 1 or result.returncode:
        raise LibraryError(response.get('error', 'Original import could not finish') if isinstance(response, dict) else 'Original import could not finish')
