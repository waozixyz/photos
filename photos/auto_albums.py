"""Opt-in people rules for regular albums; never change original files."""
import json
import re

from .library import LibraryError, packed

HASH = re.compile(r'[0-9a-f]{64}')


def export_memberships(library, photo):
    provenance = json.loads(photo['edits']).get('auto_albums',{})
    return {row['name']:identities(provenance[row['id']]) for row in library.db.execute('SELECT id,name FROM albums WHERE deleted=0')
            if row['id'] in provenance and row['name'] in json.loads(photo['albums'])}


def import_memberships(library, photo_id, value):
    if not isinstance(value,dict) or len(value)>128:
        raise LibraryError('Invalid automatic album memberships')
    parsed = {library.album_name(name):identities(selected) for name,selected in value.items()}
    photo = library.photo(photo_id); edits = json.loads(photo['edits'])
    for name, selected in parsed.items():
        if name in json.loads(photo['albums']) and name not in edits.get('removed_albums',[]):
            album = library._ensure_album(name)
            edits.setdefault('auto_albums',{})[album['id']]=selected
    library.db.execute('UPDATE photos SET edits=? WHERE id=?',(packed(edits),photo_id))


def identities(value):
    if not isinstance(value,list) or len(value)>64 or any(not isinstance(i,str) or not HASH.fullmatch(i) for i in value):
        raise LibraryError('Choose up to 64 people for this album')
    return sorted(set(value))


def validate_provenance(value):
    if not isinstance(value,dict) or len(value)>128 or any(not isinstance(i,str) or not HASH.fullmatch(i) for i in value):
        raise LibraryError('Invalid automatic album memberships')
    return {album:identities(selected) for album,selected in value.items()}


def configure(library, name, selected, remove=False):
    selected=identities(selected)
    if type(remove) is not bool:
        raise LibraryError('Choose whether to keep previously added photos')
    from . import native_edit
    native_edit.people_change(library,'configure',name=library.album_name(name),ids=selected,remove=remove)


def apply(library, photo_id=None, album_id=None):
    from . import native_edit
    native_edit.people_change(library,'apply',photo=photo_id or '',album=album_id or '')
