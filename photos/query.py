"""Compatibility service adapter for the native Ziran SQLite query engine."""
import json
import os
from pathlib import Path
import subprocess

from .library import LibraryError, packed


def page(library, **request):
    package = Path(__file__).resolve().parents[1]
    candidates = [package / 'library_query', package / 'build/library-query/library_query', package / 'library-query/library_query']
    program = next((path for path in candidates if path.is_file()), None)
    if program is None:
        raise LibraryError('Build or install the Photos gallery engine before browsing')
    environment = {key: value for key, value in os.environ.items() if key not in {
        'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER'}}
    try:
        result = subprocess.run([str(program), str(library.root / 'library.sqlite')], input=packed(request).encode(),
                                env=environment, capture_output=True, timeout=30)
        response = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise LibraryError('The gallery query could not finish; try again') from None
    if result.returncode or response.get('error'):
        raise LibraryError(response.get('error', 'The gallery query could not finish; try again'))
    return response


def validate(library, search):
    if not isinstance(search, str) or not search.strip() or len(search.encode()) > 512 or '\0' in search:
        raise LibraryError('Choose a nonempty search of at most 512 bytes')
    page(library, view='validate', search=search, sort='newest')
    return search.strip()
