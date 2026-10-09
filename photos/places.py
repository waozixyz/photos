"""Named areas use the existing encrypted saved-album record format."""
import json
import math

from .library import LibraryError


def coordinates(metadata):
    values = []
    for key, maximum in [('latitude', 90), ('longitude', 180)]:
        geo = metadata.get('geoData')
        value = geo.get(key) if isinstance(geo, dict) else None
        if value is None:
            geo = metadata.get('geoDataExif')
            value = geo.get(key) if isinstance(geo, dict) else None
        if type(value) not in {int, float} or not math.isfinite(value) or not -maximum <= value <= maximum:
            return None
        values.append(value)
    return values


def area(value):
    if not isinstance(value, str) or len(value.encode()) > 128 or '\0' in value:
        raise LibraryError('Enter latitude, longitude, radius in kilometres')
    try:
        parts = [float(v.strip()) for v in value.split(',')]
    except (ValueError, OverflowError):
        parts = []
    if len(parts) != 3 or not all(math.isfinite(v) for v in parts) or not (
            -90 <= parts[0] <= 90 and -180 <= parts[1] <= 180 and .001 <= parts[2] <= 20040):
        raise LibraryError('Use valid coordinates and a radius from 0.001 to 20040 km')
    return 'near:' + ','.join(format(v, '.15g') for v in parts)


def is_place(search):
    if not isinstance(search, str) or not search.startswith('near:'):
        return False
    try:
        return area(search[5:]) == search
    except LibraryError:
        return False


def create(library, value, photo_id=''):
    try:
        decoded = json.loads(value)
        if not isinstance(decoded, list) or len(decoded) != 2:
            raise ValueError()
        name, coordinates = decoded
    except (TypeError, ValueError):
        raise LibraryError('Choose a name and area for this place') from None
    search = area(coordinates)
    return library.edit_album('', 'smart_create', name, search=search, source=photo_id)


def update(library, name, value):
    if not is_place(library.album_details(name)['filter']):
        raise LibraryError('Choose a named place to change its area')
    return library.edit_album(name, 'filter', area(value))
