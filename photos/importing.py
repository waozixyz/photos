"""Compatibility adapter for native immutable-original import."""
import json
import os
from pathlib import Path
import re
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


def merge(library, request):
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
    if result.returncode or response.get('saved') != 1 or response.get('added') not in (0, 1):
        raise LibraryError('Import outcome could not be confirmed; rescan before retrying')
    return bool(response['added'])


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
