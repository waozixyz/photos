"""Compatibility adapter for native immutable-original import."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .library import LibraryError


def program():
    root = Path(__file__).resolve().parents[1]
    paths = [root/'import_original', root/'build/import-original/import_original']
    helper = next((path for path in paths if path.is_file()), None)
    if helper is None:
        raise LibraryError('Build or install the Photos importer before importing originals')
    return helper


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
